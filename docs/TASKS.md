# Implementation Tasks

Phased breakdown of the work in [`PLAN.md`](PLAN.md). Phases are roughly
dependency-ordered; several can run in parallel (noted in the plan).

> **Tracking convention:** As phases complete, update the status column
> here AND in the agent's own todo store. Keep this file as the
> human-readable source of truth.

| #  | Status      | Phase                                                              | Depends on |
|----|-------------|--------------------------------------------------------------------|------------|
| 0  | done ✅      | Spike: verify `.mcp.json` stdio plugin packaging with hello-world  | —          |
| 1  | done ✅      | Plugin scaffolding (`.claude-plugin/`, `.mcp.json`, `README`, `install.ps1`) | 0    |
| 2  | done ✅      | Data folder + migration runner (`$HOME\.copilot\np-agent-memory\`) | 1          |
| 3  | done ✅      | MCP server skeleton (WAL, per-call connections, explicit `agent_cwd` param + canonicalization, MCP `roots` capability probe, `agent_register`, `agent_describe`) | 2 |
| 4  | done ✅      | Memory + todos tools (`memory_log`, `memory_query`, `memory_export`, `todo_*`) | 3 |
| 5  | done ✅      | Blockers + handovers tools (`blocker_*`, `handover_save`, `handover_latest`, `handover_export`, `handover_claim`, `handover_ack`, `handover_release`) | 3 |
| 6  | done ✅      | Inbox tools (`inbox_send`, `inbox_check`, `inbox_ack`)             | 3          |
| 7  | done ✅      | Backup machinery (SQLite online backup API, throttled, lazy)       | 3          |
| 8  | done ✅      | Bundled skill `skills/agent-memory/SKILL.md`                       | 4, 5, 6    |
| 9  | pending     | Rewrite Connects `ingest-handovers` skill (claim/ack model + `source_*` columns + uniqueness) | 5 |
| 10 | pending     | Rewrite global `handover-report` skill (dual-write transition, then `handover_save` only) | 5 |
| 11 | pending     | Update each workstream's `copilot-instructions.md` with a "Use agent-memory" section | 8 |
| 12 | pending     | Optional one-time backfill of historical handover markdown files   | 10         |

## Decided up front (per planning session, 2026-05-25)

- **Server language:** Python (with bundled venv via `install.ps1`)
- **Repo location:** `C:\path\to\NP.CoPilot.AgentMemory`
- **Distribution:** shareable from day one (`marketplace.json` included)
- **Inbox addressing:** accept both canonical path and registered name
- **Backfill:** yes, behind a `--backfill` flag
- **Dashboard:** out of scope for v1 (data model supports a follow-up)
- **Handover-doc cutover:** dual-write transition, not hard cutover

## Identity model — agents never see IDs

- `agents.id` is an immutable ULID — internal, used as the FK target.
- **Agents pass their own working directory.** The server cannot derive it
  itself (inside a plugin-launched MCP stdio process, `os.getcwd()` is the
  plugin install dir, not the agent's repo — confirmed in `docs/spike-0.md`
  §6). Every tool that scopes to the calling agent takes an explicit
  `agent_cwd: str` parameter.
- The agent author calls `agent_register(name, workstream, agent_cwd)` at
  session start. The server canonicalizes the supplied path (absolute →
  resolve symlinks → normalize Windows case → strip trailing separators →
  forward-slash for storage) and looks it up in `agent_aliases` to return
  the right ULID.
- Choosing the *right* path is the agent's responsibility (the bundled
  skill teaches `git rev-parse --show-toplevel` for git-backed agents).
- One agent can have multiple alias paths (e.g., canonical Q-drive path +
  OneDrive symlink path), all resolving to the same ULID.
- Moves / renames just add another alias row. No FK rewrites.
- **Phase 3 probe (done — see `docs/spike-roots.md`):** the Copilot CLI
  (`github-copilot-developer` 1.0.57-3, protocol 2025-11-25) does **not**
  advertise MCP `roots`, and `list_roots()` returns `Method not found`.
  Decision: `agent_cwd` stays **required** on every agent-scoped tool; no
  `roots` fallback was built. Re-probe is cheap if a future CLI adds `roots`.

## Crash-safety: the two-phase handover ack

The naive "return + mark consumed" dequeue loses data if the consumer crashes
between read and write. Use a claim → process → ack model:

1. `handover_claim(consumer_id)` sets `claimed_at` + `claimed_by`, returns rows.
2. Consumer (e.g., Connects ingest) writes the data to its own store with
   `source_system='np-agent-memory'`, `source_table='handovers'`,
   `source_id=<handover.id>` (Connects has a UNIQUE constraint on those three
   for idempotency).
3. `handover_ack(ids)` sets `consumed_at`.
4. Claims older than N minutes are returnable to other consumers — no data
   is silently lost on crash.

`handover_release(id, last_error)` lets a consumer cleanly back off without
waiting for the timeout.
