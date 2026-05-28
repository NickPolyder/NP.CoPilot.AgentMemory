"""Migration runner for the agent-memory SQLite database.

Design:
* Migration files live in this package directory as 0001_name.sql, 0002_name.sql, etc.
* Each file is applied in a single BEGIN IMMEDIATE transaction.
* A `migrations` table tracks which versions have been applied (version + SHA-256 checksum).
* Multi-process safe: BEGIN IMMEDIATE ensures only one process wins the race.
  Losers get SQLITE_BUSY, retry with backoff, and then see the migration already applied.
* Checksums are verified on startup: if a previously-applied migration file has been
  modified, the server refuses to start (guards against accidental edits to shipped SQL).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).parent

_MIGRATION_PATTERN = re.compile(r"^(\d{4})_.+\.sql$")

# Retry budget for BEGIN IMMEDIATE contention
_MAX_RETRIES = 5
_BASE_DELAY_S = 0.1  # doubles each retry


def _discover_migrations() -> list[tuple[int, Path]]:
    """Find and sort migration files in ascending version order."""
    results: list[tuple[int, Path]] = []
    for f in sorted(_MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_PATTERN.match(f.name)
        if m:
            results.append((int(m.group(1)), f))
    return results


def _checksum(sql: str) -> str:
    """SHA-256 hex digest of migration content.

    Normalizes line endings (CRLF → LF) and strips trailing whitespace to
    ensure consistent hashes regardless of git autocrlf settings or OS.
    """
    normalized = sql.strip().replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _split_statements(sql: str) -> list[str]:
    """Split SQL text into individual statements, respecting quoted strings.

    Handles:
    * Semicolons inside single-quoted string literals (e.g., 'hello;world')
    * Multi-line statements with leading comment lines (stripped from front)
    * Comment-only segments (discarded)
    """
    statements: list[str] = []
    current: list[str] = []
    in_quote = False

    for char in sql:
        if char == "'" and not in_quote:
            in_quote = True
            current.append(char)
        elif char == "'" and in_quote:
            # Handle escaped quotes ('') by peeking isn't needed — SQLite
            # uses '' for literal single quotes, and toggling in_quote twice
            # for adjacent quotes produces the same correct split behavior.
            in_quote = False
            current.append(char)
        elif char == ";" and not in_quote:
            # End of statement
            raw_stmt = "".join(current)
            stmt = _strip_leading_comments(raw_stmt)
            if stmt:
                statements.append(stmt)
            current = []
        else:
            current.append(char)

    # Handle trailing content (no final semicolon)
    if current:
        raw_stmt = "".join(current)
        stmt = _strip_leading_comments(raw_stmt)
        if stmt:
            statements.append(stmt)

    return statements


def _strip_leading_comments(raw_stmt: str) -> str:
    """Strip leading blank/comment-only lines from a raw statement chunk."""
    cleaned_lines: list[str] = []
    for line in raw_stmt.splitlines():
        stripped = line.strip()
        if not cleaned_lines and (not stripped or stripped.startswith("--")):
            continue
        cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip()


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
    """Open a connection configured for migration work."""
    from np_agent_memory.db import _configure_connection

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    _configure_connection(conn)
    return conn


def run_migrations(db_path: Path) -> None:
    """Apply pending migrations to the database.

    Multi-process safe: uses BEGIN IMMEDIATE with retry/backoff.
    Verifies checksums of previously applied migrations.
    """
    migrations = _discover_migrations()
    if not migrations:
        return

    conn = _open_migration_connection(db_path)
    try:
        _ensure_migrations_table(conn)
        applied = _applied_versions(conn)

        for version, path in migrations:
            sql = path.read_text(encoding="utf-8")
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

            # Apply with retry for multi-process contention
            _apply_migration(conn, version, sql, cs, path.name)

    finally:
        conn.close()


def _apply_migration(
    conn: sqlite3.Connection,
    version: int,
    sql: str,
    checksum: str,
    filename: str,
) -> None:
    """Apply a single migration inside BEGIN IMMEDIATE with retry.

    All DDL + the migrations record insert run atomically in one transaction.
    This prevents the race where executescript() implicitly commits and
    releases the write lock mid-migration.
    """
    statements = _split_statements(sql)

    for attempt in range(_MAX_RETRIES):
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Double-check inside the transaction (another process may have won)
                row = conn.execute(
                    "select version from migrations where version = ?", (version,)
                ).fetchone()
                if row is not None:
                    conn.execute("ROLLBACK")
                    print(
                        f"[np-agent-memory] migration {filename}: already applied (race ok)",
                        file=sys.stderr,
                        flush=True,
                    )
                    return

                # Execute each statement individually within the transaction
                for stmt in statements:
                    conn.execute(stmt)

                # Record the migration in the same transaction — atomic
                conn.execute(
                    "insert into migrations (version, checksum, applied_at) values (?, ?, ?)",
                    (version, checksum, datetime.now(timezone.utc).isoformat()),
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
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < _MAX_RETRIES - 1:
                delay = _BASE_DELAY_S * (2**attempt)
                print(
                    f"[np-agent-memory] migration {filename}: locked, retry {attempt + 1}/{_MAX_RETRIES} in {delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(delay)
            else:
                raise
