# Probe — MCP `roots` capability (Phase 3 gate)

**Status:** ✅ Complete — **`roots` NOT supported. `agent_cwd` stays REQUIRED.**

**Date:** 2026-05-30
**Copilot CLI MCP client:** `github-copilot-developer` `1.0.57-3`
**MCP protocol version:** `2025-11-25`
**MCP SDK (server):** `mcp==1.26.0`

---

## 1. Why this probe

`docs/PLAN.md` §"Implementation phases" #3 and the `docs/spike-0.md` §6
follow-up mandate a ~30-minute investigation, **before** Phase 3 hardens the
tool surface, of whether the Copilot CLI advertises the MCP `roots`
capability. If it did, the server could read `mcp.roots()` once per session
and accept `agent_cwd` as optional. If not, the explicit per-call
`agent_cwd: str` parameter remains the contract.

`roots` is a **client** capability, declared by the MCP client (the Copilot
CLI) in its `initialize` handshake. It cannot be inferred from a local test
client — only the real CLI connecting to our server reveals it.

## 2. Method

A temporary diagnostic tool `roots_probe` was added to the server. It reads
`ctx.session.client_params.capabilities` and attempts a real
`session.list_roots()` round-trip. The current `server/np_agent_memory`
package was synced into the installed plugin dir
(`~/.copilot/installed-plugins/np-agent-memory-marketplace/np-agent-memory`)
and invoked headlessly:

```powershell
copilot -p "Call the np-agent-memory-roots_probe tool and output its raw JSON" --allow-all
```

## 3. Result

```json
{
  "client_info": { "name": "github-copilot-developer", "version": "1.0.57-3" },
  "protocol_version": "2025-11-25",
  "capabilities_raw": {
    "elicitation": { "form": {}, "url": {} },
    "tasks": { "requests": { "tools": { "call": {} } } },
    "extensions": { "io.modelcontextprotocol/ui": { "mimeTypes": ["text/html;profile=mcp-app"] } }
  },
  "roots_advertised": false,
  "roots_list_changed": null,
  "list_roots_ok": false,
  "list_roots_error": "McpError: Method not found"
}
```

- The client advertises `elicitation`, `tasks`, and a UI `extensions` block.
- **No `roots` capability** is present.
- An actual `list_roots()` call fails with `McpError: Method not found` — the
  CLI does not implement the server→client `roots/list` request at all.

## 4. Decision

- **`agent_cwd: str` stays REQUIRED** on every agent-scoped tool. There is no
  `roots`-based fallback to build.
- The wire shape is exactly what `PLAN.md` already specifies, so no plan
  amendment is needed — only this confirmation.
- Re-probe is cheap (re-run the one command) if a future CLI version starts
  advertising `roots`; revisit then, not now.

## 5. Cleanup

- The temporary `roots_probe` tool was removed from
  `server/np_agent_memory/__main__.py` after this writeup.
