# Custom Instructions — NP.CoPilot.AgentMemory

This repository implements `np-agent-memory`: a Copilot CLI plugin providing
a shared SQLite-backed memory + inbox for every agent on Nick's machine.

## Agent role in this repo

You are the **implementing agent**. Your job is to deliver the plugin per
[`docs/PLAN.md`](../docs/PLAN.md), phase by phase, in
[`docs/TASKS.md`](../docs/TASKS.md) order.

- The **Connects agent** (in `C:\path\to\Connects`) tracks progress
  and Nick's broader workstreams — it does **not** implement this plugin.
- The **infra-agent**, **analytics-agent**, **ops-agent**, **security-agent**,
  etc. own their own domains — they do **not** implement this plugin.
- You handle everything in this repo: the MCP server, the schema, the
  plugin manifest, the bundled skill, the rewrites of the global
  `handover-report` skill and the Connects `ingest-handovers` skill once
  the server is ready.

## Hard rules

- **Phase 0 (the spike) MUST pass before any real code is written.** Build
  a 1-tool hello-world plugin, install it, confirm the Copilot CLI launches
  the stdio server, confirm working-dir / env / arg semantics. Document the
  result in `docs/spike-0.md` before moving on.
- **Runtime data lives OUTSIDE the plugin install directory.** The DB,
  backups, and logs live at `$HOME\.copilot\np-agent-memory\` (configurable
  via `AGENT_MEMORY_DIR`). The plugin install dir under
  `$HOME\.copilot\installed-plugins\_direct\np-agent-memory\` may be wiped
  on reinstall.
- **Agents never see internal IDs.** Every tool resolves the calling agent
  from its canonicalized working directory via `agent_aliases`. ULIDs are
  used only as FK targets internally.
- **Multi-process from day one.** Assume the Copilot CLI may launch one MCP
  server per CLI window. Per-call connections, `BEGIN IMMEDIATE` migrations,
  no shared in-memory state, busy-timeout retries.
- **Two-phase handover ack** for Connects ingest — never use a single-call
  "read + mark consumed" pattern. Use `handover_claim` → process →
  `handover_ack`, with stale claim timeout and `handover_release` for
  clean backoff.
- **Required `limit` on every list/query tool**, server-capped, with cursor
  pagination. Truncate large bodies (`body_md`) unless `full=true`.

## Style

- **Python style:** match the conventions in the user-level instructions at
  `C:\path\to\NP.CoPilot.Config\instructions\` (auto-loaded). Type
  hints everywhere. Black-style formatting. Small focused modules in
  `server/tools/`.
- **PowerShell style:** `$ErrorActionPreference = 'Stop'`, idempotent
  scripts, emoji status output.
- **SQL style:** lowercase keywords. Two-space indent. One CHECK constraint
  per line. Index names use `idx_<table>_<columns>` pattern.

## Commits

- Conventional Commits, imperative mood.
- Include the `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`
  trailer.
- Group commits by phase. Use `feat(phase-N): ...` or `chore(phase-N): ...`
  prefixes.

## Handover at session end

When you wrap a session in this repo, save a handover via the (eventually
plugin-provided) `handover_save` tool. Until that tool exists, use the
current `handover-report` skill to write a markdown handover so the Connects
agent can ingest your progress.

## Don't

- Don't modify files in `C:\path\to\Connects` (that's Nick's journal
  repo — coordinate via the Connects agent, not direct edits).
- Don't ship a built artifact that requires Nick to manually `pip install`
  anything — `install.ps1` must produce a working venv with pinned deps.
- Don't store secrets in the repo. The plugin holds no secrets in v1.
- Don't break out of the phased plan without writing a short ADR in
  `docs/decisions/` explaining why.
