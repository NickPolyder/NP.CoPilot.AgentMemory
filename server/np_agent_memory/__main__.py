"""np-agent-memory MCP server entry point.

Phase 1 scope: load cleanly under the Copilot CLI plugin loader and expose
ONE diagnostic tool (`memory_alive`) so the agent can verify the plugin
wired up correctly. Real tools (agent_register, memory_log, todo_*,
handover_*, inbox_*) ship from Phase 3 onwards once the schema, migration
runner, and agent_cwd contract are in place.

Conventions:
* Per-call connections, WAL, busy_timeout — start landing in Phase 2/3.
  Not relevant yet (no DB access in Phase 1).
* Every agent-scoped tool MUST accept an explicit `agent_cwd: str` per the
  identity-model amendment in docs/PLAN.md (`memory_alive` is intentionally
  server-scoped so it does NOT take `agent_cwd`).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory import __version__ as PACKAGE_VERSION

try:
    from importlib.metadata import version as _pkg_version

    _MCP_SDK_VERSION = _pkg_version("mcp")
except Exception as exc:  # pragma: no cover - defensive
    _MCP_SDK_VERSION = f"unknown ({exc!r})"


_STARTED_AT = time.time()
_STARTED_AT_ISO = datetime.now(timezone.utc).isoformat(timespec="microseconds")


mcp = FastMCP(
    name="np-agent-memory",
    instructions=(
        "Shared persistent memory + cross-agent inbox + handover transport "
        "for every Copilot CLI agent. Phase 1: only `memory_alive` is "
        "available; real tools land from Phase 3."
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
        "phase": "1 - scaffolding (no DB, no agent tools yet)",
    }


def main() -> None:
    # Stderr breadcrumb so a bad start shows up in
    # ~/.copilot/logs/process-<unix-ms>-<pid>.log (the CLI captures stderr
    # under `[mcp server np-agent-memory stderr]`). See docs/spike-0.md §6.
    print(
        f"[np-agent-memory] starting: pid={os.getpid()} "
        f"version={PACKAGE_VERSION} exe={sys.executable!r}",
        file=sys.stderr,
        flush=True,
    )
    mcp.run()


if __name__ == "__main__":
    main()
