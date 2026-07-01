# ADR 0008 — Make the `escalated` blocker state reachable via `blocker_escalate`

- **Status:** Accepted
- **Date:** 2026-07-01
- **Decider:** Nick Polyderopoulos
- **Related:** [`docs/PLAN.md`](../PLAN.md),
  [`server/np_agent_memory/tools/blockers.py`](../../server/np_agent_memory/tools/blockers.py),
  [`skills/agent-memory/SKILL.md`](../../skills/agent-memory/SKILL.md)

## Context

The `blockers` schema has always modeled three states —
`active` / `escalated` / `resolved` — with a dedicated `escalated_at` column and
a CHECK constraint permitting `escalated`. The tool layer advertised `escalated`
too: it is a value of the `blocker_list` `status` filter Literal and is counted
as "active" in `describe_agent`'s active-blocker summary.

But **no code path ever set it.** `blocker_open` creates `active`,
`blocker_resolve` sets `resolved`, and nothing in between. So `escalated` was
dead surface: advertised in the schema, the enum, and the describe count, yet
unreachable — a state agents were told about but could never enter. A full-panel
review flagged this as a correctness/consistency gap.

## Decision

Add a first-class `blocker_escalate(agent_cwd, blocker_id, reason=None)` tool
(and core `escalate_blocker`) that moves an **active** blocker to `escalated`,
stamps `escalated_at`, and auto-logs a timeline note (mirroring how
`blocker_open` / `blocker_resolve` log). Escalation is a deliberate one-way
signal:

- from `active` → `escalated` (stamps `escalated_at`);
- escalating an already-`escalated` blocker raises (not idempotent churn);
- escalating a `resolved` blocker raises;
- only the owning agent (resolved via `require_agent_id` + `agent_id = ?`) can
  escalate its own blocker.

The optional `reason` is folded into the auto-logged note for context.

No migration is needed — the column, CHECK value, and enum already exist; this
change only makes the existing state reachable.

## Consequences

### Positive

- The advertised `escalated` state is now real: agents can raise a blocker's
  visibility, it shows up under the `status="escalated"` filter, and the
  describe active-count stays meaningful.
- Consistent with existing blocker verbs (auto-logs a note, agent-scoped,
  guarded transitions).

### Negative

- One more tool in the surface agents must understand.

### Neutral / forward-looking

- A direct `escalated` → `resolved` path already works via `blocker_resolve`
  (it accepts any non-resolved status). A "de-escalate" back to `active`, and
  routing an escalation to another agent's inbox, are possible follow-ups left
  out of scope here.
