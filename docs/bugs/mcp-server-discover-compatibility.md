# MCP `server/discover` compatibility (SDK v1 → v2 migration)

**Date:** 2026-09-03

## Problem

The current Copilot CLI host's MCP client defaults to an era-neutral
connection mode: on every new connection it first sends a `server/discover`
request (introduced by the 2026-07-28 MCP specification revision) to probe
which protocol revision the server speaks, and only falls back to the older
`initialize` handshake if discovery is unsupported.

`np-agent-memory` was pinned to MCP Python SDK v1 (`mcp==1.26.0`), which
implements only the pre-2026-07-28 `initialize` handshake and has no handler
for `server/discover`. Each connection from the current host therefore
generated an unrecognized-method rejection and then downgraded to the legacy
handshake.

## Evidence

- A sanitized diagnostic bundle from the host (SHA-256
  `F7DE35995E6212E145D07F9666AD80D1235E6235DA0211589BBB92DAB70A6C1A`) contained
  17 literal validation errors, each produced by MCP SDK 1.26.0 rejecting a
  `server/discover` request it does not implement.
- The [MCP Python SDK v2 migration guide](https://py.sdk.modelcontextprotocol.io/migration/)
  documents this exact failure mode under *"`Client` defaults to
  `mode='auto'`"*: **"servers log an unexpected `server/discover` request"**
  when a v2 client talks to a v1 server.
- Reproduced locally with the SDK's own in-memory `Client` testing pattern:
  against a v1 server, a default (`mode="auto"`) client falls back to
  `initialize`; against the migrated v2 `MCPServer`, the same client receives
  a `server/discover` response and negotiates protocol revision `2026-07-28`.

## Root cause

MCP Python SDK v1 predates the 2026-07-28 specification revision entirely —
it only ever speaks the connection-oriented `initialize` handshake. The
current host's client asks first, unconditionally, whether a server supports
the newer stateless-discovery model; a v1 server has no code path for that
request at all, so the request fails as an unrecognized method. The host
recovers by falling back to `initialize`, but leaves the validation errors in
its diagnostics and cannot use the modern stateless protocol. The dependency
pin (`mcp==1.26.0`) was the entire cause: nothing in the server's own tool
logic was involved.

## Correction

- Raised the `mcp` dependency in [`pyproject.toml`](../../pyproject.toml)
  from `1.26.0` to `2.1.1` — the current MCP Python SDK v2 stable line, which
  supports the 2026-07-28 revision (including `server/discover`) while still
  serving legacy `2025-11-25`-and-earlier `initialize` clients.
- Ported every `from mcp.server.fastmcp import FastMCP` import to
  `from mcp.server import MCPServer` (the v2 rename) across the entry point
  and all tool-registration modules, and updated the corresponding
  `register_*_tools(mcp: FastMCP)` signatures/docstrings to `MCPServer`:
  - `server/np_agent_memory/__main__.py`
  - `server/np_agent_memory/backup.py`
  - `server/np_agent_memory/tools/__init__.py`
  - `server/np_agent_memory/tools/{agents,blockers,handovers,inbox,memory,todos}.py`
- Updated `server/tests/test_agents.py`, which builds a standalone probe
  server to assert on published tool schemas, to construct `MCPServer`
  instead of `FastMCP` and read `Tool.input_schema` instead of the v1
  camelCase `Tool.inputSchema` (v2 fields are snake_case; see the migration
  guide's *"Field names changed from camelCase to snake_case"* section).
- Set the deployed transport explicitly with
  `mcp.run(transport="stdio")` in `server/np_agent_memory/__main__.py`.
  SDK v2 moves transport selection from the constructor to `run()`, so this
  prevents a future SDK-default change from silently changing how the plugin
  launches; `server/tests/test_main.py` pins the argument.
- The server already passed `name=` and `instructions=` to the constructor by
  keyword (v2 reorders positional parameters). The codebase never touches the
  low-level `Server`, `ClientSession`, or `mcp.types` camelCase attributes
  directly.
- Rebuilt the repo-local `.venv` with `uv sync --extra dev`, which also
  resolved and pinned the full v2 dependency graph in a new `uv.lock`
  (`mcp-types` exact-pinned to the SDK version, `httpx2`, `opentelemetry-api`,
  and raised floors on `anyio`/`sse-starlette`/`typing-extensions`/`pywin32`
  — all introduced or changed by v2, none touched by hand).

## Validation

```powershell
# Full targeted + full suite, post-migration
.\.venv\Scripts\python.exe -m pytest server\tests -q
# 372 passed, 0 failed

.\.venv\Scripts\python.exe -m ruff check server
# All checks passed!

.\.venv\Scripts\python.exe -m ruff format --check server
# 30 files already formatted
```

A new regression suite,
[`server/tests/test_discovery.py`](../../server/tests/test_discovery.py),
uses the SDK's official in-memory `Client` transport for the negotiation
matrix and one subprocess stdio test for the deployed launch path. It proves:

1. A default (`mode="auto"`) client completes discovery and negotiates
   protocol revision `2026-07-28` — the exact path that previously failed
   against the v1 server.
2. A client pinned to `mode="legacy"` still completes the pre-2026-07-28
   `initialize` handshake, so existing hosts are not stranded by the upgrade.
3. Tools remain listable (`list_tools()`) and callable
   (`call_tool("memory_alive", {})`) over the discovery-negotiated session.
4. The deployed stdio transport answers `server/discover` directly rather
   than falling back to `initialize`.
