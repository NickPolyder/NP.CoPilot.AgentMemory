"""Database layer: data folder provisioning and connection factory.

Phase 2 scope:
* Resolve the runtime data directory ($HOME/.copilot/np-agent-memory/ or
  AGENT_MEMORY_DIR override).
* Provision the folder structure (db, backups/, logs/) on first access.
* Provide a connection factory that configures WAL, busy_timeout, and
  foreign keys on every connection.
* Per-call connections — no connection pooling, no shared in-memory state.
  Multiple MCP server processes may hit the same DB concurrently.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
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

# Persistent pragmas (survive connection close) vs per-connection pragmas.
# journal_mode=WAL is persistent once set; the others are per-connection.
_PRAGMAS = [
    "PRAGMA journal_mode = WAL;",
    "PRAGMA foreign_keys = ON;",
    "PRAGMA busy_timeout = 5000;",
    "PRAGMA synchronous = NORMAL;",
]


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply runtime pragmas to a fresh connection.

    Shared by both the connection factory and the migration runner to avoid
    pragma drift.
    """
    for pragma in _PRAGMAS:
        conn.execute(pragma)


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
    _configure_connection(conn)
    return conn


@contextmanager
def open_connection(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that yields a configured connection and closes it on exit."""
    conn = connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Startup helper (called once per server process)
# ---------------------------------------------------------------------------


def init_db(data_dir: Path | None = None) -> Path:
    """One-time initialization: ensure dirs exist, run migrations, return db path.

    Called once at MCP server startup. Subsequent tool calls use open_connection().
    """
    from np_agent_memory.migrations import run_migrations

    data_dir = ensure_data_dir(data_dir)
    db_path = get_db_path(data_dir)

    print(
        f"[np-agent-memory] db: {db_path}",
        file=sys.stderr,
        flush=True,
    )

    run_migrations(db_path)
    return db_path
