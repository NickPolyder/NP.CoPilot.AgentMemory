"""Phase 0 hello-world MCP server for the np-agent-memory plugin.

ONE tool: spike_ping. Returns everything we need to characterize the runtime
environment the Copilot CLI gives a plugin-launched stdio MCP server. Used
to validate Phase 0 of np-agent-memory before any production code is written.
"""

from __future__ import annotations

import os
import re
import sys
import time
import platform
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

try:
    from importlib.metadata import version as _pkg_version
    _MCP_VERSION = _pkg_version("mcp")
except Exception as exc:  # pragma: no cover
    _MCP_VERSION = f"unknown ({exc!r})"


_STARTED_AT = time.time()
_STARTED_AT_ISO = datetime.now(timezone.utc).isoformat(timespec="microseconds")
_INVOCATION_COUNT = 0


# Env vars we want to surface. We deliberately allow-list (not deny-list) so we
# never accidentally leak a secret from the user's environment.
_ENV_KEY_PATTERN = re.compile(
    r"^("
    r"COPILOT|CLAUDE|MCP|PLUGIN|WORKSPACE|PWD|CWD|ROOT|GITHUB|USERPROFILE|"
    r"HOMEPATH|HOMEDRIVE|HOME|PYTHON|VIRTUAL_ENV|SPIKE|AGENT_MEMORY|"
    r"OLDPWD|INIT_CWD|TERM_PROGRAM|VSCODE|WT_SESSION"
    r").*$",
    re.IGNORECASE,
)


def _filtered_env() -> dict[str, str]:
    return {
        key: value
        for key, value in sorted(os.environ.items())
        if _ENV_KEY_PATTERN.match(key)
    }


mcp = FastMCP(
    name="np-agent-memory-spike",
    instructions=(
        "Phase 0 spike server for the np-agent-memory plugin. Call spike_ping "
        "to learn what the Copilot CLI plugin runtime looks like (server cwd, "
        "argv, env, pid, sdk version)."
    ),
)


@mcp.tool()
def spike_ping(caller_cwd: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Return a structured snapshot of the server's runtime environment.

    Args:
        caller_cwd: Optional. The agent's *own* current working directory, as
            the agent sees it. We need this because the server's os.getcwd()
            is set by the Copilot CLI plugin loader (typically the plugin
            install dir), NOT the agent/terminal CWD. The production plugin's
            identity model depends on knowing the agent's CWD — this probes
            whether agents can reliably pass it in.
        note: Optional. Free-form text echoed back, useful when calling the
            tool multiple times in one session to verify per-call behavior.

    Returns:
        A dict describing the server process, its working directory, its
        executable / venv layout, filtered environment, MCP SDK version,
        invocation counter (probes process reuse), and the args the caller
        passed.
    """
    global _INVOCATION_COUNT
    _INVOCATION_COUNT += 1

    cwd = os.getcwd()
    env_filtered = _filtered_env()
    path_value = os.environ.get("PATH", "")

    return {
        "server": {
            "cwd": cwd,
            "executable": sys.executable,
            "sys_prefix": sys.prefix,
            "sys_base_prefix": sys.base_prefix,
            "sys_version": sys.version,
            "argv": sys.argv,
            "module_file": __file__,
            "pid": os.getpid(),
            "ppid": os.getppid() if hasattr(os, "getppid") else None,
            "platform": platform.platform(),
            "started_at_iso": _STARTED_AT_ISO,
            "uptime_seconds": round(time.time() - _STARTED_AT, 3),
            "invocation_count": _INVOCATION_COUNT,
        },
        "mcp": {
            "sdk": "mcp",
            "sdk_version": _MCP_VERSION,
            "server_name": mcp.name,
        },
        "env": {
            "filtered": env_filtered,
            "filtered_count": len(env_filtered),
            "path_head": path_value[:200],
            "path_length": len(path_value),
            "path_separator": os.pathsep,
            "path_sep": os.sep,
        },
        "args": {
            "caller_cwd_echo": caller_cwd,
            "note_echo": note,
        },
        "time": {
            "utc_iso": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
            "monotonic": time.monotonic(),
        },
    }


def main() -> None:
    # Diagnostic line on stderr so we know the process started, even if the
    # MCP handshake fails. Stderr from plugin-launched MCP servers may or may
    # not be captured by the CLI — that's one of the things this spike checks.
    print(
        f"[spike_server] starting: pid={os.getpid()} "
        f"cwd={os.getcwd()!r} exe={sys.executable!r}",
        file=sys.stderr,
        flush=True,
    )
    mcp.run()


if __name__ == "__main__":
    main()
