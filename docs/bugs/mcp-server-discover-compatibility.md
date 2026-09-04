# MCP protocol mismatch after SDK v2 upgrade

**Date:** 2026-09-04

## Problem

The `mcp==2.1.1` upgrade changed the server to MCP protocol `2026-07-28`.

The installed Copilot CLI still sends the `2025-11-25` `initialize` handshake after probing the server.

The v2 server rejects that handshake, so the host cannot initialize the `np-agent-memory` MCP connection.

```text
connection is serving the 2026-07-28 protocol; the initialize handshake is not accepted
```

## Root cause

The prior regression suite used the MCP SDK v2 in-memory client.

That client can emulate a legacy handshake against an `MCPServer`, but it did not reproduce the Copilot CLI's discovery-then-initialize sequence.

Consequently, the tests incorrectly established that the v2 migration remained compatible with the CLI.

## Correction

- Restore `mcp==1.26.0` and use its `FastMCP` server implementation.
- Retain the explicit stdio transport.
- Replace the in-memory v2 negotiation tests with a subprocess stdio test using the v1 `ClientSession` initialization handshake.

The v1 server does not implement `server/discover`; the CLI can safely fall back to the `2025-11-25` handshake that it supports.

## Follow-up

Do not upgrade to the MCP v2 server until a Copilot CLI version completes a modern `2026-07-28` session instead of sending the legacy `initialize` handshake after discovery.
