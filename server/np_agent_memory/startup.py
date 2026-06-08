"""Startup orchestration: provision dirs, run migrations, return the DB path.

This is the one place that ties the low-level connection/data-dir layer
(``np_agent_memory.db``) together with the migration runner
(``np_agent_memory.migrations``). Keeping it here — rather than inside ``db`` —
makes the dependency direction a clean DAG: ``startup -> {db, migrations}`` and
``migrations -> db``, with no edge back into ``db``.
"""

from __future__ import annotations

import sys
from pathlib import Path

from np_agent_memory.db import ensure_data_dir, get_db_path
from np_agent_memory.migrations import run_migrations


def init_db(data_dir: Path | None = None) -> Path:
    """One-time initialization: ensure dirs exist, run migrations, return db path.

    Called once at MCP server startup. Subsequent tool calls use
    ``np_agent_memory.db.open_connection()``.
    """
    data_dir = ensure_data_dir(data_dir)
    db_path = get_db_path(data_dir)

    print(
        f"[np-agent-memory] db: {db_path}",
        file=sys.stderr,
        flush=True,
    )

    run_migrations(db_path)
    return db_path
