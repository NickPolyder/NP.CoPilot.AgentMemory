"""Migration runner for the agent-memory SQLite database.

Design:
* Migration files live in this package directory as 0001_name.sql, 0002_name.sql, etc.
* Each file is applied atomically in a single Python-owned BEGIN IMMEDIATE transaction.
* SQLite's own parser runs the DDL via ``executescript`` — no hand-rolled SQL
  splitting — so quoted identifiers, triggers with internal semicolons, and
  comments are tokenized exactly as SQLite intends. The migration connection is
  opened with ``autocommit=True`` so the Python-owned BEGIN IMMEDIATE survives
  ``executescript`` (legacy ``isolation_level=None`` force-commits it instead).
* The migrations row is double-checked under the write lock BEFORE the DDL runs,
  so a process that loses the cold-start race never executes the migration body.
  This keeps non-idempotent migrations safe.
* A `migrations` table tracks applied versions (version + SHA-256 checksum).
* Multi-process safe: BEGIN IMMEDIATE ensures only one process wins the race.
  Losers get SQLITE_BUSY, retry with backoff+jitter, and then see the
  migration already applied.
* Checksums are verified on startup: if a previously-applied migration file has been
  modified, the server refuses to start (guards against edits to shipped SQL).
* Fresh connection per migration: avoids corrupted connection state after a
  failed rollback.

Migration file conventions:
* Pure DDL — must NOT contain transaction-control statements (BEGIN/COMMIT/
  ROLLBACK/SAVEPOINT/RELEASE/END). The runner owns the transaction; such a
  statement is rejected at PREPARE time by a SQLite authorizer before it can
  run, so a malformed body can never commit partial DDL.
* Must NOT contain statements that require running outside a transaction
  (e.g. VACUUM, PRAGMA journal_mode): the whole file executes inside the
  runner-owned BEGIN IMMEDIATE, where such statements error or become no-ops.
* Must form syntactically complete statements (no unterminated comments or
  string literals) — enforced via ``sqlite3.complete_statement`` before apply.
"""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from np_agent_memory.db import WalConversionError

_MIGRATIONS_DIR = Path(__file__).parent

_MIGRATION_PATTERN = re.compile(r"^(\d{4,})_.+\.sql$")

# Retry budget for BEGIN IMMEDIATE / cold-start WAL contention. Sized with
# headroom for high-fan-out cold starts (many CLI windows launched at once all
# serializing through the first WAL conversion + 0001 apply). Worst-case tail
# latency (~a few seconds) is paid once, only on first init under heavy
# contention.
_MAX_RETRIES = 8
_BASE_DELAY_S = 0.1  # doubles each retry, with jitter


def _discover_migrations() -> list[tuple[int, Path]]:
    """Find and sort migration files in ascending version order.

    Raises RuntimeError if two files share the same version number, which
    would otherwise cause one migration to be silently skipped.
    """
    results: list[tuple[int, Path]] = []
    seen: dict[int, str] = {}
    for f in sorted(_MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_PATTERN.match(f.name)
        if m and f.is_file():
            version = int(m.group(1))
            if version in seen:
                raise RuntimeError(
                    f"Duplicate migration version {version:04d}: "
                    f"{seen[version]!r} and {f.name!r}. Each version must be unique."
                )
            seen[version] = f.name
            results.append((version, f))
    # Sort by parsed integer version, not filename string order, so a future
    # 5+ digit version (e.g. 10000_x.sql) still orders after 0999_x.sql.
    results.sort(key=lambda item: item[0])
    return results


def _is_busy_error(e: sqlite3.OperationalError) -> bool:
    """Return True if an OperationalError represents a transient lock.

    Prefers the structured sqlite_errorcode (Python 3.11+) over brittle
    message-substring matching, falling back to substrings on older runtimes.
    """
    code = getattr(e, "sqlite_errorcode", None)
    if code is not None:
        # Mask off extended result bits: extended codes such as
        # SQLITE_BUSY_SNAPSHOT (517) or SQLITE_LOCKED_SHAREDCACHE (262) share
        # the primary class in the low byte. SQLITE_BUSY = 5, SQLITE_LOCKED = 6.
        # SQLITE_LOCKED is included as transient: in this file-based,
        # no-shared-cache, fresh-connection-per-attempt design it is rare, and
        # treating it as retryable is the safe-conservative choice (worst case
        # burns the backoff budget before failing, never masks a real error).
        return (code & 0xFF) in (5, 6)
    msg = str(e).lower()
    return "database is locked" in msg or "database table is locked" in msg


def _checksum(sql: str) -> str:
    """SHA-256 hex digest of migration content.

    Normalizes line endings (CRLF → LF) and strips trailing whitespace to
    ensure consistent hashes regardless of git autocrlf settings or OS.
    """
    normalized = sql.strip().replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _validate_migration_sql(sql: str, filename: str) -> None:
    """Reject migration SQL that does not form complete statements.

    Guards against the silent-truncation failure mode where an unterminated
    block comment or string literal causes SQLite's parser to discard the
    remainder of the file without error (and the migration would still be
    recorded as applied). ``sqlite3.complete_statement`` uses SQLite's own
    tokenizer, so it agrees with how ``executescript`` will read the file.
    A trailing ``;`` is appended so a final statement without its own
    terminator (valid SQL) is still treated as complete; empty/comment-only
    files are allowed (they apply as a no-op).
    """
    probe = sql.strip()
    if probe and not sqlite3.complete_statement(probe + "\n;"):
        raise RuntimeError(
            f"Migration {filename} contains incomplete SQL "
            f"(unterminated comment or string literal?). Refusing to apply."
        )


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the migrations tracking table if it doesn't exist."""
    conn.execute("""
        create table if not exists migrations (
            version    integer primary key,
            checksum   text not null,
            applied_at text not null
        )
    """)


def _applied_versions(conn: sqlite3.Connection) -> dict[int, str]:
    """Return {version: checksum} for all applied migrations."""
    rows = conn.execute("select version, checksum from migrations").fetchall()
    return {row[0]: row[1] for row in rows}


def _open_migration_connection(db_path: Path) -> sqlite3.Connection:
    """Open a connection configured for migration work.

    Opened with ``autocommit=True`` (PEP 249 autocommit mode, Python 3.12+) so a
    Python-issued ``BEGIN IMMEDIATE`` survives a subsequent ``executescript`` call.
    Legacy ``isolation_level=None`` would force-commit the open transaction at the
    start of ``executescript``, defeating atomicity.

    Uses a shorter busy_timeout (1s) than normal connections (5s) to fail
    faster on true deadlocks while still tolerating brief transient locks.
    The retry loop with backoff provides the real contention tolerance.

    Closes the connection if configuration fails so a failed open never
    leaks a descriptor.
    """
    from np_agent_memory.db import _configure_connection

    conn = sqlite3.connect(str(db_path), autocommit=True)
    try:
        _configure_connection(conn)
        # Override busy_timeout: migrations rely on explicit retry/backoff,
        # so a shorter timeout avoids 5s × retries worst-case stall.
        conn.execute("PRAGMA busy_timeout = 1000;")
    except Exception:
        conn.close()
        raise
    return conn


def _retry_on_busy(
    fn: Callable[[sqlite3.Connection], None],
    db_path: Path,
    label: str,
) -> None:
    """Execute a function with retry/backoff on SQLITE_BUSY.

    Opens a fresh connection per attempt INSIDE the retry scope, so a lock
    encountered while configuring the connection (e.g. the delete->WAL
    conversion) is also retried rather than crashing startup.
    """
    for attempt in range(_MAX_RETRIES):
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_migration_connection(db_path)
            fn(conn)
            return
        except (sqlite3.OperationalError, WalConversionError) as e:
            retryable = isinstance(e, WalConversionError) or _is_busy_error(e)
            if retryable and attempt < _MAX_RETRIES - 1:
                delay = _BASE_DELAY_S * (2**attempt) * (0.5 + random.random())
                print(
                    f"[np-agent-memory] {label}: database busy or WAL setup "
                    f"contended, retry {attempt + 1}/{_MAX_RETRIES} in "
                    f"{delay:.2f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
            else:
                raise
        finally:
            if conn is not None:
                conn.close()


def run_migrations(db_path: Path) -> None:
    """Apply pending migrations to the database.

    Multi-process safe: uses BEGIN IMMEDIATE with retry/backoff+jitter.
    Verifies checksums of previously applied migrations.
    Each migration uses a fresh connection to avoid corrupted state after
    failed rollbacks.
    """
    migrations = _discover_migrations()
    if not migrations:
        return

    # Bootstrap: ensure migrations table exists (with retry for concurrent startup)
    def _bootstrap(conn: sqlite3.Connection) -> None:
        _ensure_migrations_table(conn)

    _retry_on_busy(_bootstrap, db_path, "bootstrap")

    # Read applied versions (with retry — autocommit read can hit BUSY on fresh WAL)
    applied: dict[int, str] = {}

    def _read_applied(conn: sqlite3.Connection) -> None:
        nonlocal applied
        applied = _applied_versions(conn)

    _retry_on_busy(_read_applied, db_path, "read versions")

    # Integrity guards (fail loud before applying anything). Both conditions
    # require developer error to trigger, but catch silent, hard-to-debug schema
    # divergence:
    #   1. An applied version whose file was removed — the checksum loop below
    #      only iterates discovered files, so a deleted applied migration would
    #      otherwise be ignored, defeating the "shipped SQL never changes" guard.
    #   2. A pending version numbered below the highest applied version — it
    #      would apply out of order, building schema in the wrong sequence.
    discovered = {version for version, _ in migrations}
    missing = sorted(set(applied) - discovered)
    if missing:
        raise RuntimeError(
            f"Applied migration version(s) {missing} have no file on disk — "
            f"refusing to start. Do NOT delete shipped migration files."
        )
    max_applied = max(applied, default=0)
    out_of_order = sorted(
        version
        for version, _ in migrations
        if version not in applied and version < max_applied
    )
    if out_of_order:
        raise RuntimeError(
            f"Pending migration version(s) {out_of_order} are numbered below the "
            f"already-applied version {max_applied} — refusing to apply out of "
            f"order. New migrations must use a higher version number."
        )

    for version, path in migrations:
        # utf-8-sig strips a leading BOM if present, so a BOM-prefixed file and
        # a plain UTF-8 file produce identical text (and identical checksums).
        sql = path.read_text(encoding="utf-8-sig")
        cs = _checksum(sql)

        if version in applied:
            # Verify checksum hasn't changed
            if applied[version] != cs:
                raise RuntimeError(
                    f"Migration {path.name} checksum mismatch! "
                    f"Expected {applied[version][:12]}…, got {cs[:12]}…. "
                    f"Do NOT edit shipped migration files — create a new one."
                )
            continue  # already applied

        # Reject incomplete SQL before acquiring any lock (clear early failure).
        _validate_migration_sql(sql, path.name)

        # Apply with retry for multi-process contention (fresh conn per attempt)
        _apply_migration(db_path, version, sql, cs, path.name)


def _deny_transaction_control(
    action: int,
    _arg1: str | None,
    _arg2: str | None,
    _db_name: str | None,
    _trigger: str | None,
) -> int:
    """SQLite authorizer that rejects transaction and database-attach control.

    ``SQLITE_TRANSACTION`` covers BEGIN/COMMIT/ROLLBACK/END/RELEASE and
    ``SQLITE_SAVEPOINT`` covers SAVEPOINT/RELEASE. Denying these at *prepare*
    time means the forbidden statement is rejected before it executes — so the
    runner-owned BEGIN IMMEDIATE stays open and the surrounding ROLLBACK undoes
    any DDL the body already executed before the forbidden statement. This is a
    hard guarantee, unlike the post-hoc ``in_transaction`` check which only
    fires after partial work may already have been committed.

    ``SQLITE_ATTACH``/``SQLITE_DETACH`` are also denied: an attached database is
    outside the runner-owned transaction's atomicity boundary (and ATTACH can
    create an external file that an outer ROLLBACK would not clean up), so
    migrations must operate only on the main database.
    """
    if action in (
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_SAVEPOINT,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _apply_migration(
    db_path: Path,
    version: int,
    sql: str,
    checksum: str,
    filename: str,
) -> None:
    """Apply a single migration inside BEGIN IMMEDIATE with retry.

    All DDL + the migrations record insert run atomically in one transaction.
    The connection is opened with ``autocommit=True`` so the Python-owned
    BEGIN IMMEDIATE survives ``executescript``. The migrations row is
    double-checked under the write lock BEFORE any DDL runs, so a race-loser
    never executes the migration body (keeping non-idempotent migrations safe).
    Uses a fresh connection per attempt to avoid corrupted state after rollback.
    """
    for attempt in range(_MAX_RETRIES):
        conn: sqlite3.Connection | None = None
        try:
            conn = _open_migration_connection(db_path)
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Double-check inside the transaction, BEFORE running any DDL
                # (another process may have won the race). Compare the checksum
                # too: if the winner applied a *different* body for this version,
                # that's a real conflict, not a benign race.
                row = conn.execute(
                    "select checksum from migrations where version = ?", (version,)
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK")
                    existing = row[0]
                    if existing != checksum:
                        raise RuntimeError(
                            f"Migration {filename} checksum mismatch after race! "
                            f"Another process applied {existing[:12]}…, "
                            f"we have {checksum[:12]}…."
                        )
                    print(
                        f"[np-agent-memory] migration {filename}: "
                        "already applied (race ok)",
                        file=sys.stderr,
                        flush=True,
                    )
                    return

                # SQLite's own parser runs the whole file. In autocommit mode
                # this does NOT commit our BEGIN IMMEDIATE — the transaction
                # stays open for the bookkeeping insert + COMMIT below.
                #
                # An authorizer rejects any transaction-control statement
                # (BEGIN/COMMIT/ROLLBACK/END/SAVEPOINT/RELEASE) at PREPARE time,
                # before it can execute. Without this, a stray COMMIT would
                # close our transaction mid-script and permanently commit
                # partial DDL that we could no longer roll back. The authorizer
                # is scoped to executescript only and cleared immediately after,
                # so our own bookkeeping COMMIT below is not blocked.
                conn.set_authorizer(_deny_transaction_control)
                try:
                    conn.executescript(sql)
                except sqlite3.DatabaseError as e:
                    # An authorizer denial surfaces as DatabaseError. Prefer the
                    # structured errorcode (SQLITE_AUTH = 23) over a brittle
                    # message-substring match, falling back to the substring for
                    # older runtimes / wording changes.
                    is_auth_denial = (
                        getattr(e, "sqlite_errorcode", None) == sqlite3.SQLITE_AUTH
                        or "not authorized" in str(e).lower()
                    )
                    if is_auth_denial:
                        raise RuntimeError(
                            f"Migration {filename} contains a forbidden statement "
                            f"(transaction control BEGIN/COMMIT/ROLLBACK/END/"
                            f"SAVEPOINT/RELEASE, or ATTACH/DETACH). Migration "
                            f"files must operate only on the main database and "
                            f"must not manage transactions — the runner owns the "
                            f"transaction."
                        ) from e
                    raise
                finally:
                    conn.set_authorizer(None)

                # Belt-and-suspenders: with the authorizer active above, every
                # transaction-control statement is denied at PREPARE time, so
                # this branch is unreachable by design. It is kept as a cheap
                # second line of defense — if a future change ever narrows the
                # authorizer, this still refuses to record a migration whose body
                # closed the runner-owned transaction, before we rely on it for
                # atomic bookkeeping.
                if not conn.in_transaction:
                    raise RuntimeError(
                        f"Migration {filename} closed its own transaction. "
                        f"Migration files must not contain BEGIN/COMMIT/ROLLBACK."
                    )

                # Record the migration in the same transaction — atomic
                conn.execute(
                    "insert into migrations (version, checksum, applied_at) "
                    "values (?, ?, ?)",
                    (version, checksum, datetime.now(UTC).isoformat()),
                )
                conn.execute("COMMIT")

                print(
                    f"[np-agent-memory] migration {filename}: applied ✓",
                    file=sys.stderr,
                    flush=True,
                )
                return

            except Exception:
                try:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        except (sqlite3.OperationalError, WalConversionError) as e:
            retryable = isinstance(e, WalConversionError) or _is_busy_error(e)
            if retryable and attempt < _MAX_RETRIES - 1:
                delay = _BASE_DELAY_S * (2**attempt) * (0.5 + random.random())
                print(
                    f"[np-agent-memory] migration {filename}: database busy or "
                    f"WAL setup contended, retry {attempt + 1}/{_MAX_RETRIES} in "
                    f"{delay:.2f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
            else:
                raise
        finally:
            if conn is not None:
                conn.close()
