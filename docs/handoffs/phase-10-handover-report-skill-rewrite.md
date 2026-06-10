# Phase 10 — Rewrite the global `handover-report` skill to call `handover_save`

### 🔄 Handoff: np-agent-memory implementing agent → global-skills owner

**Reason:** Session handovers now live in the shared `np-agent-memory` SQLite
store, not in markdown files. The global `handover-report` skill
(`~/.copilot/skills/handover-report/SKILL.md`) must become a thin wrapper that
calls `handover_save(...)` instead of writing a file. This is global Copilot CLI
config, outside the np-agent-memory repo.

**Priority:** Advisory.

---

## Context

- `handover_save` is **agent-scoped**, so it needs `agent_cwd` (the calling
  agent's canonical repo root). Resolve it the same way the bundled
  [`agent-memory` skill](../../skills/agent-memory/SKILL.md) teaches:
  `git rev-parse --show-toplevel` for git-backed agents, else a stable
  per-workstream root.
- The agent must be registered first (`agent_register`) — the bundled skill's
  session-start snippet handles this. The `handover-report` skill should assume
  registration happened, but fail gracefully (prompt the agent to register) if
  `handover_save` returns "agent_cwd is not registered".
- **The structured-body markdown shape stays identical** so the human-readable
  output is unchanged — only the sink changes (DB row instead of file).

## Request

Replace the body of `~/.copilot/skills/handover-report/SKILL.md` so that, when
invoked, it:

1. Resolves `agent_cwd` (canonical repo root).
2. Composes the **same structured handover body** it produces today.
3. Calls:
   ```text
   handover_save(
     agent_cwd = <canonical root>,
     summary   = "<one-line session summary>",
     body_md   = "<the structured markdown body>",
     session_id = <optional current session id>
   )
   ```
4. Reports the returned handover `id` + `saved_at` to the user. **Writes no
   file.**

### Structured body template to preserve

Keep whatever sections the current skill emits. The canonical shape is:

```markdown
## What I did
- …

## Current state
- …

## Next steps
- …

## Decisions / notes
- …

## Open questions / blockers
- …
```

## Constraints / locked decisions

- **No file writes.** The DB is the single source of truth; Connects ingests it
  via the claim/ack protocol (phase 9). Writing a duplicate markdown file would
  double-ingest.
- Preserve the existing section headings/order so downstream parsing and human
  reading are unaffected.
- Tools are namespaced `np-agent-memory-handover_save` in the CLI.
- To let the user re-read a saved handover, point them at
  `np-agent-memory-handover_latest` / `handover_export` rather than reopening a
  file.

## Done when

- Invoking `handover-report` persists a handover via `handover_save` and writes
  no markdown file.
- `np-agent-memory-handover_latest(agent_cwd)` returns the just-saved handover.
- The rendered markdown (via `handover_export`) matches the previous file
  format.
