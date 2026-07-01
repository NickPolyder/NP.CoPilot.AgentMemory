# ADR 0007 — Handover ingest gets a dead-letter (quarantine) state

- **Status:** Accepted
- **Date:** 2026-07-01
- **Decider:** Nick Polyderopoulos
- **Related:** [`docs/PLAN.md`](../PLAN.md),
  [`server/np_agent_memory/tools/handovers.py`](../../server/np_agent_memory/tools/handovers.py),
  [`server/np_agent_memory/migrations/0003_add_handovers_quarantine.sql`](../../server/np_agent_memory/migrations/0003_add_handovers_quarantine.sql),
  [`skills/agent-memory/SKILL.md`](../../skills/agent-memory/SKILL.md)

## Context

Handovers use a two-phase claim/ack transport (ADR-aligned with the "never a
single read + mark consumed" hard rule). A consumer (e.g. Connects
`ingest-handovers`) claims a batch, ingests it, then `handover_ack`s on success
or `handover_release`s on failure. A released claim is immediately reclaimable.

That loop has no terminal failure state: a **poison** handover — one that always
fails to ingest (malformed body, a bug in the consumer, an oversized payload) —
is claimed, released, reclaimed, released… forever. It never leaves the
claimable pool, wastes every ingest cycle, and can starve healthy handovers
behind it. There was no way to say "this one is broken, stop retrying it, let a
human look."

## Options considered

### Option A — Attempt-capped quarantine on release (chosen)

`attempt_count` is already incremented once per claim. When `handover_release`
is called and `attempt_count >= _MAX_CLAIM_ATTEMPTS`, the handover is moved to a
terminal **quarantined** state (`quarantined_at` stamped) instead of being
returned to the pool. Quarantined rows are excluded from `handover_claim` and
surfaced for triage via a new read-only `handover_quarantined` tool.

- **Pros**
  - Bounds retries: a poison payload dead-letters after a fixed number of failed
    rounds instead of looping forever.
  - **Additive / non-breaking for the existing consumer.** A healthy consumer
    acks on success and never reaches the cap, so its behavior is unchanged. The
    Connects ingest contract does not have to change to keep working; it only
    *gains* the option to inspect quarantine.
  - Nothing is lost — quarantined rows stay in the table and are inspectable
    (with `last_error`, `claimed_by`, `quarantined_at`, `attempt_count`) so a
    human/consumer can triage. Forensic claim info is deliberately preserved.
  - Tiny schema change (one nullable column + a partial index over quarantined
    rows).
- **Cons**
  - Adds a state to the handover lifecycle and a new tool to learn.
  - No in-band requeue path yet: un-quarantining is manual (out of scope here).

### Option B — Drop poison handovers after N attempts

- **Pros:** simplest; no new column or tool.
- **Cons:** destroys data the plugin exists to preserve; a transient bug that
  looks like poison would silently lose real handovers. Rejected.

### Option C — Leave it; rely on the consumer to stop retrying

- **Pros:** zero server change.
- **Cons:** pushes dead-letter bookkeeping into every consumer, off-server,
  where it is easy to get wrong; the infinite-retry starvation stays possible.
  Rejected.

## Decision

**Adopt Option A.** Add `quarantined_at` (migration `0003`), exclude quarantined
rows from claiming, quarantine on a release at/beyond `_MAX_CLAIM_ATTEMPTS`
(set to 5), and add the read-only `handover_quarantined` inspection tool
(cross-agent, keyset-paginated, body truncated unless `full=true`). The release
response reports `quarantined` / `quarantined_ids` separately from `released`.

## Consequences

### Positive

- Poison handovers can no longer loop forever or starve the queue.
- The change is backward-compatible: existing consumers keep working without
  modification and can adopt quarantine inspection when ready.

### Negative

- One more lifecycle state and tool to reason about.

### Neutral / forward-looking

- An explicit **requeue** tool (clear `quarantined_at` to make a triaged
  handover claimable again) and a retention/purge job for long-quarantined rows
  are natural follow-ups, intentionally out of scope here.
