"""np-agent-memory MCP server entry point.

Phase 2 scope: data folder provisioning, migration runner, and DB init on
startup. The server now creates its runtime directory, applies versioned
SQL migrations, and exposes the DB path in `memory_alive`. Real agent-scoped
tools (agent_register, memory_log, todo_*, handover_*, inbox_*) ship from
Phase 3 onwards.

Conventions:
* Per-call connections, WAL, busy_timeout — active from this phase.
* Every agent-scoped tool MUST accept an explicit `agent_cwd: str` per the
  identity-model amendment in docs/PLAN.md (`memory_alive` is intentionally
  server-scoped so it does NOT take `agent_cwd`).
"""

from __future__ import annotations

import sys

# Fail fast with an actionable message on unsupported Python BEFORE importing
# version-specific names below: datetime.UTC requires 3.11 and
# sqlite3.connect(autocommit=True) requires 3.12, so on an older interpreter a
# direct `python -m np_agent_memory` launch would otherwise die with a cryptic
# ImportError/TypeError. requires-python only constrains installers, not a
# direct launch under a stale interpreter, so this runtime guard must stay.
if sys.version_info < (3, 12):  # noqa: UP036
    print(
        f"[np-agent-memory] FATAL: Python 3.12+ is required, running "
        f"{sys.version_info.major}.{sys.version_info.minor}. "
        f"Re-create the plugin venv with Python 3.12 or newer.",
        file=sys.stderr,
        flush=True,
    )
    sys.exit(1)

import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory import __version__ as PACKAGE_VERSION
from np_agent_memory.db import init_db

try:
    from importlib.metadata import version as _pkg_version

    _MCP_SDK_VERSION = _pkg_version("mcp")
except Exception as exc:  # pragma: no cover - defensive
    _MCP_SDK_VERSION = f"unknown ({exc!r})"


_STARTED_AT = time.time()
_STARTED_AT_ISO = datetime.now(UTC).isoformat(timespec="microseconds")

# Initialized in main(); None until then (allows import without side effects).
_DB_PATH: Path | None = None


mcp = FastMCP(
    name="np-agent-memory",
    instructions=(
        "Shared persistent memory + cross-agent inbox + handover transport "
        "for every Copilot CLI agent. Phase 2: DB initialized with full "
        "schema; agent-scoped tools land in Phase 3."
    ),
)


@mcp.tool()
def memory_alive() -> dict[str, Any]:
    """Server-scoped liveness probe.

    Returns minimal metadata identifying the server build and process. Does
    NOT take an `agent_cwd` because it is not agent-scoped (it tells you
    about the server, not about any one agent).

    Returns:
        A dict with server name, package version, MCP SDK version, pid,
        executable path, server-start ISO timestamp, and uptime seconds.
    """
    return {
        "server_name": mcp.name,
        "package_version": PACKAGE_VERSION,
        "mcp_sdk_version": _MCP_SDK_VERSION,
        "pid": os.getpid(),
        "executable": sys.executable,
        "started_at_iso": _STARTED_AT_ISO,
        "uptime_seconds": round(time.time() - _STARTED_AT, 3),
        "db_path": str(_DB_PATH) if _DB_PATH else None,
        "phase": "2 - data folder + migrations (agent tools in Phase 3)",
    }


def main() -> None:
    global _DB_PATH

    # Stderr breadcrumb so a bad start shows up in
    # ~/.copilot/logs/process-<unix-ms>-<pid>.log (the CLI captures stderr
    # under `[mcp server np-agent-memory stderr]`). See docs/spike-0.md §6.
    print(
        f"[np-agent-memory] starting: pid={os.getpid()} "
        f"version={PACKAGE_VERSION} exe={sys.executable!r}",
        file=sys.stderr,
        flush=True,
    )

    # Initialize DB before accepting tool calls. Structured error handling
    # ensures users see actionable diagnostics rather than raw tracebacks.
    try:
        _DB_PATH = init_db()
    except sqlite3.OperationalError as e:
        # Permissions, disk-full mid-write, locked, and corruption surface
        # here (NOT as OSError) — give a filesystem-oriented hint.
        print(
            f"[np-agent-memory] FATAL: database error during DB init: {e}\n"
            f"  Hint: check the data directory is writable, the disk is not "
            f"full, and the database file is not locked or corrupt.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except sqlite3.Error as e:
        print(
            f"[np-agent-memory] FATAL: sqlite error during DB init: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except OSError as e:
        print(
            f"[np-agent-memory] FATAL: filesystem error during DB init: {e}\n"
            f"  Hint: check that the data directory is writable and disk is not full.",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except RuntimeError as e:
        print(
            f"[np-agent-memory] FATAL: {e}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)
    except Exception as e:
        print(
            f"[np-agent-memory] FATAL: unexpected error during DB init: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    mcp.run()


if __name__ == "__main__":
    main()
