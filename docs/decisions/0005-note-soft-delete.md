# ADR 0005 — Allow deleting notes (soft by default, hard on confirmation)

- **Status:** Accepted
- **Date:** 2026-06-30
- **Decider:** Nick Polyderopoulos
- **Related:** [`docs/PLAN.md`](../PLAN.md),
  [`server/np_agent_memory/tools/memory.py`](../../server/np_agent_memory/tools/memory.py),
  [`server/np_agent_memory/migrations/0002_add_notes_deleted_at.sql`](../../server/np_agent_memory/migrations/0002_add_notes_deleted_at.sql),
  [`skills/agent-memory/SKILL.md`](../../skills/agent-memory/SKILL.md)

## Context

`notes` was specified as an **append-only event stream** (see
[`docs/PLAN.md`](../PLAN.md)): agents log progress / decisions / notes and never
mutate them. That stance kept the model simple and the timeline trustworthy.

In practice two needs broke the absolutism:

1. **Junk accumulates.** Diagnostic / test notes (e.g. the stuck
   `01KWCQZ6QSFHWNCN7HEJ30DC9A` `[DIAGNOSTIC]` note on the primary machine) and
   mistaken logs have no removal path. The only "fix" was reaching into SQLite by
   hand — exactly what the plugin exists to avoid.
2. **No retraction.** An agent that logs the wrong thing (wrong category, wrong
   content) can append a correction but cannot remove the misleading original, so
   the timeline stays noisy.

The append-only purity was a means (a trustworthy, skimmable timeline), not an
end. A controlled delete that defaults to reversible serves that end better than
forbidding deletion entirely.

## Options considered

### Option A — Soft delete by default, explicit hard delete (chosen)

`memory_delete(agent_cwd, ids, hard=false)`:

- **Soft (default):** stamp a `deleted_at` timestamp. The note disappears from
  `memory_query` / `memory_export` but the row survives and can be surfaced with
  `memory_query(include_deleted=true)` or hard-deleted later.
- **Hard (`hard=true`):** permanently `DELETE` the row.

Only the calling agent's own notes are affected (resolved via `require_agent_id`
+ `agent_id = ?`).

- **Pros**
  - Reversible by default — a fat-fingered delete loses nothing.
  - Still gives a real purge path for genuine junk.
  - Agent-scoped: no agent can delete another's history.
  - Tiny schema change (one nullable column + a partial index for the
    alive-notes query).
- **Cons**
  - Reverses the documented append-only stance (this ADR records that).
  - "Confirm with the user before hard delete" cannot be enforced by an stdio
    MCP server (it can't prompt) — it relies on the agent honoring the
    instruction. Mitigated below.

### Option B — Hard delete only

- **Pros:** simplest; no `deleted_at` column.
- **Cons:** every delete is irreversible; a wrong `ids` argument destroys real
  history with no recovery. Too sharp for an agent-driven tool.

### Option C — Keep append-only; prune out-of-band

- **Pros:** preserves the original invariant; zero new surface.
- **Cons:** the actual problem (junk, no retraction) stays unsolved; forces
  manual SQLite surgery — the anti-goal of the plugin.

## Decision

**Adopt Option A.** Add a single `memory_delete` tool covering all categories
(`progress` / `decision` / `note` all live in the `notes` table). Default to
**soft** delete; require an explicit `hard=true` for permanent removal.

### Implementation

- **Migration `0002_add_notes_deleted_at.sql`** — add `deleted_at text` (null =
  alive) and a partial index `idx_notes_agent_time_alive` over alive rows so the
  common "list my live notes" query stays cheap.
- **`memory.py`** — `query_memory` / `_fetch_notes` and `export_memory` gain
  `include_deleted` (default `false`, so existing reads transparently hide
  soft-deleted notes); export marks any included soft-deleted note _(deleted)_.
  Every note response now carries a `deleted_at` field (null for live notes) so
  callers can tell live from soft-deleted. New `delete_notes(agent_cwd, ids,
  hard)` returns `{mode, deleted, deleted_ids, skipped (already soft-deleted),
  not_found (not your notes)}`; soft delete is idempotent. `hard` must be a real
  boolean — a non-bool (e.g. the string `"false"`) is rejected so a malformed
  caller cannot fall through to the irreversible path. `ids` is de-duplicated and
  capped (`_MAX_NOTE_IDS`).
- **`restore_notes(agent_cwd, ids)` / `memory_restore`** — the inverse of a soft
  delete: clears `deleted_at` so notes reappear. Cannot recover a hard-deleted
  row. This makes the "reversible" property of soft delete real rather than
  aspirational.
- **Hard-delete confirmation is policy, not a server prompt.** The `hard`
  parameter description and the `memory_delete` docstring loudly instruct the
  agent to confirm with the user before passing `hard=true`; `SKILL.md`
  repeats it. The server cannot block on a prompt over stdio, so the guardrail
  lives where the model reads it.

## Consequences

### Positive

- Agents can clean up junk and retract mistakes through the plugin, with no
  manual SQLite access.
- The default path is non-destructive and genuinely reversible: a soft-deleted
  note can be inspected with `include_deleted=true` and brought back with
  `memory_restore`.
- Existing callers are unaffected — `include_deleted` defaults to hiding deleted
  notes, matching the prior behavior.

### Negative

- The append-only invariant is gone. Tooling/docs that leaned on "notes are
  never removed" are updated (`SKILL.md` reworded from "append-only" to
  "append-mostly", with a delete section).
- Hard delete is irreversible and only soft-guarded by instruction; a
  misbehaving agent could destroy its own notes. Scope is limited to the calling
  agent, so blast radius is one agent's history.

### Neutral / forward-looking

- `memory_restore` (clear `deleted_at`) ships with this change so soft delete is
  truly reversible. A retention job that hard-prunes long-soft-deleted rows, and
  capturing a `deleted_by` / reason for audit, remain natural follow-ups that are
  intentionally out of scope here.
