"""Database layer: data folder provisioning and connection factory.

* Resolve the runtime data directory ($HOME/.copilot/np-agent-memory/ or
  AGENT_MEMORY_DIR override).
* Provision the folder structure (db, backups/, logs/) on first access.
* Provide a connection factory that configures WAL, busy_timeout, and
  foreign keys on every connection.
* Per-call connections — no connection pooling, no shared in-memory state.
  Multiple MCP server processes may hit the same DB concurrently.

This module is the lowest layer: it depends on nothing else in the package.
Startup orchestration (ensure dirs -> run migrations) lives in
``np_agent_memory.startup`` so that ``db`` never imports ``migrations`` (the
dependency direction is ``startup -> {db, migrations}`` and ``migrations ->
db``).
"""

from __future__ import annotations

import os
import random
import sqlite3
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from pathlib import Path

# ---------------------------------------------------------------------------
# Data directory resolution
# ---------------------------------------------------------------------------

_DEFAULT_RELATIVE = Path(".copilot") / "np-agent-memory"
_DB_FILENAME = "agent-memory.db"


def get_data_dir() -> Path:
    """Resolve the runtime data directory.

    Priority:
      1. AGENT_MEMORY_DIR env var (absolute path).
      2. $HOME/.copilot/np-agent-memory/

    Raises ValueError if the resulting path is relative or HOME is unset.
    """
    override = os.environ.get("AGENT_MEMORY_DIR")
    if override:
        data_dir = Path(override)
        if not data_dir.is_absolute():
            raise ValueError(
                f"AGENT_MEMORY_DIR must be an absolute path, got: {override!r}"
            )
        return data_dir

    home = Path.home()
    return home / _DEFAULT_RELATIVE


def ensure_data_dir(data_dir: Path | None = None) -> Path:
    """Ensure the runtime data directory and subdirectories exist.

    Creates: data_dir/, data_dir/backups/, data_dir/logs/
    Returns the data directory path.
    """
    if data_dir is None:
        data_dir = get_data_dir()

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "backups").mkdir(exist_ok=True)
    (data_dir / "logs").mkdir(exist_ok=True)

    return data_dir


def get_db_path(data_dir: Path | None = None) -> Path:
    """Return the full path to the SQLite database file."""
    if data_dir is None:
        data_dir = get_data_dir()
    return data_dir / _DB_FILENAME


# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


class WalConversionError(RuntimeError):
    """Raised when ``PRAGMA journal_mode=WAL`` did not take effect.

    The delete->WAL conversion can fail transiently when another process holds
    a lock during checkpoint (it may return ``'delete'`` instead of raising
    SQLITE_BUSY). This is treated as retryable cold-start contention by the
    migration runner's busy-retry loop; on a single non-retrying connection it
    propagates as a fatal RuntimeError (its base class).
    """


# Persistent pragmas (survive connection close) vs per-connection pragmas.
# journal_mode=WAL is persistent once set; the others are per-connection.
# synchronous=NORMAL with WAL: writes sync only on checkpoint, not every
# commit. A power failure / OS crash (not process crash) can lose the most
# recent committed transactions. Acceptable trade-off for agent-memory data.
#
# busy_timeout is set before journal_mode=WAL so it governs ordinary lock
# waits (BEGIN IMMEDIATE, statement execution) from the very first connection.
# NOTE: busy_timeout does NOT help the delete->WAL conversion itself — that
# mode change raises SQLITE_BUSY immediately under a concurrent lock and does
# not invoke the busy handler. Cold-start WAL-conversion contention is
# tolerated by the retry/backoff loop in migrations/__init__.py, NOT by this
# pragma. Do not remove that retry loop on the assumption busy_timeout covers
# it — it does not.
_PRAGMAS = [
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA synchronous = NORMAL;",
]


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply runtime pragmas to a fresh connection.

    Public, shared contract used by both the connection factory and the
    migration runner to avoid pragma drift. Verifies WAL mode was actually set
    (it can silently fail if another process holds an exclusive lock during
    checkpoint).
    """
    for pragma in _PRAGMAS:
        result = conn.execute(pragma)
        # Verify WAL mode — it returns the journal mode as a result row.
        # If it did not take effect (returns e.g. 'delete'), raise a retryable
        # WalConversionError so the migration runner's busy loop retries the
        # cold-start contention rather than degrading multi-process safety.
        if "journal_mode" in pragma:
            row = result.fetchone()
            # A missing row is anomalous — the pragma always returns the mode.
            if row is None or row[0].lower() != "wal":
                got = row[0] if row else None
                raise WalConversionError(
                    f"Failed to set WAL journal mode, got: {got!r}. "
                    f"Another process may hold an exclusive lock on the database."
                )


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a new connection to the agent-memory database.

    Applies WAL, foreign_keys, busy_timeout, and synchronous pragmas.
    Caller is responsible for closing the connection.
    """
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(
        str(db_path),
        isolation_level=None,  # autocommit by default; explicit txn control
    )
    conn.row_factory = sqlite3.Row
    # Close the connection if configuration fails (e.g. WAL verification),
    # otherwise the descriptor leaks on every failed per-call connection.
    try:
        configure_connection(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def open_connection(
    db_path: Path | None = None,
) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a configured connection and closes it on exit."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Transaction helpers (tool calls)
# ---------------------------------------------------------------------------

# Retry budget for tool-level BEGIN IMMEDIATE write contention. busy_timeout
# already governs ordinary lock waits, but multiple CLI windows can serialize
# many concurrent writers; this is belt-and-suspenders on top of busy_timeout.
# Only SQLITE_BUSY / SQLITE_LOCKED are retried — every other OperationalError
# (e.g. constraint failure, malformed SQL) propagates immediately.
_TXN_MAX_RETRIES = 6
_TXN_BASE_DELAY_S = 0.05  # doubles each retry, with jitter

# SQLite extended/primary result codes that indicate transient lock contention.
_RETRYABLE_SQLITE_CODES = frozenset({5, 6, 261, 262, 517})  # BUSY, LOCKED, *_SNAPSHOT


def _is_retryable_lock_error(exc: sqlite3.OperationalError) -> bool:
    """True if the error is transient lock contention worth retrying."""
    code = getattr(exc, "sqlite_errorcode", None)
    if code in _RETRYABLE_SQLITE_CODES:
        return True
    # Fallback for builds that do not surface sqlite_errorcode reliably.
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


def _run_in_txn[T](
    conn: sqlite3.Connection,
    work: Callable[[sqlite3.Connection], T],
    begin: str,
) -> T:
    """Run ``work(conn)`` inside a retried ``begin`` transaction.

    The whole unit of work is retried on transient lock contention so the body
    re-reads current state after losing a race (e.g. another process inserted
    the alias first). On a non-retryable error the transaction is rolled back
    and the error propagates.
    """
    last_exc: sqlite3.OperationalError | None = None
    for attempt in range(_TXN_MAX_RETRIES):
        try:
            conn.execute(begin)
            try:
                result = work(conn)
            except BaseException:
                # Guard the rollback: if SQLite already auto-rolled-back (e.g.
                # SQLITE_FULL/IOERR), an explicit ROLLBACK raises "no
                # transaction is active" and would mask the original error.
                if conn.in_transaction:
                    with suppress(sqlite3.OperationalError):
                        conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
            return result
        except sqlite3.OperationalError as exc:
            # A failed BEGIN/COMMIT may leave no open txn; roll back defensively.
            if conn.in_transaction:
                with suppress(sqlite3.OperationalError):
                    conn.execute("ROLLBACK")
            if not _is_retryable_lock_error(exc) or attempt == _TXN_MAX_RETRIES - 1:
                raise
            last_exc = exc
            delay = _TXN_BASE_DELAY_S * (2**attempt)
            time.sleep(delay + random.uniform(0, delay))
    # Unreachable: the loop either returns or raises, but satisfy type-checkers.
    raise last_exc if last_exc else RuntimeError("transaction retry loop exhausted")


def run_in_write_txn[T](
    conn: sqlite3.Connection,
    work: Callable[[sqlite3.Connection], T],
) -> T:
    """Run ``work(conn)`` inside a retried ``BEGIN IMMEDIATE`` write transaction."""
    return _run_in_txn(conn, work, "BEGIN IMMEDIATE")


def run_in_read_txn[T](
    conn: sqlite3.Connection,
    work: Callable[[sqlite3.Connection], T],
) -> T:
    """Run ``work(conn)`` inside a retried ``BEGIN DEFERRED`` read transaction.

    Gives multi-statement reads a single consistent WAL snapshot so derived
    values (e.g. several COUNT(*) queries) cannot straddle a concurrent commit.
    """
    return _run_in_txn(conn, work, "BEGIN DEFERRED")
