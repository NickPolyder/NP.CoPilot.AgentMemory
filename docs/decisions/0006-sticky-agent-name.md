# ADR 0006 — Agent names are sticky; renames are explicit

- **Status:** Accepted
- **Date:** 2026-07-01
- **Decider:** Nick Polyderopoulos
- **Related:** [`docs/PLAN.md`](../PLAN.md),
  [`server/np_agent_memory/tools/agents.py`](../../server/np_agent_memory/tools/agents.py),
  [`docs/decisions/0002-cross-agent-addressing-public-handle.md`](0002-cross-agent-addressing-public-handle.md),
  [`skills/agent-memory/SKILL.md`](../../skills/agent-memory/SKILL.md)

## Context

`agent_register` is designed to be called at session start and is idempotent on
the canonical working directory. Originally, a repeat registration updated the
agent's `name` whenever a `name` was supplied — the docstring merely *advised*
agents to omit `name` on later calls to keep the stored label.

In practice agents did not honor that advice: they re-registered every session
with a fresh, model-chosen name (role label, task-of-the-day, etc.), so an
agent's public handle drifted constantly. Because names are the human-facing
addressing key for `inbox_send` and `agent_list` (see
[ADR 0002](0002-cross-agent-addressing-public-handle.md)), a flip-flopping name
breaks discovery and cross-agent addressing, and makes the directory noisy.

The name is meant to be a stable identity, not a per-session field. Relying on
agent goodwill to keep it stable was insufficient.

## Options considered

### Option A — Sticky name + explicit `agent_rename` tool (chosen)

`agent_register` never changes the name of an already-registered agent — any
`name` passed on a repeat call is silently ignored (not an error). A dedicated
`agent_rename(agent_cwd, name)` tool is the only path that changes a name after
first registration, and its docstring instructs the agent to call it only when
the user asks.

- **Pros**
  - Identity is stable by construction, not by agent goodwill.
  - The rename intent is explicit and hard to trigger accidentally — a separate
    tool the model won't call on autopilot at session start.
  - First-registration naming (default-to-directory-name) is unchanged.
  - `workstream` / `description` keep their update-on-provide behavior; only the
    name — the addressing key — is locked down.
- **Cons**
  - New tool surface (one more tool in the catalog).
  - A passed `name` being silently ignored on re-register is a mild surprise;
    mitigated by tool/param docs and `SKILL.md`.

### Option B — `rename: bool = false` flag on `agent_register`

Keep one tool; only apply `name` when `rename=true`.

- **Pros:** no new tool.
- **Cons:** overloads the session-start register call with a rename concern; a
  model could set `rename=true` on autopilot, reintroducing drift. Less
  discoverable as "the way to rename".

### Option C — Documentation only (status quo)

Keep advising agents to omit `name` on repeat calls.

- **Pros:** zero code change.
- **Cons:** already proven insufficient — this is the behavior we're fixing.

## Decision

**Adopt Option A.** Names are sticky:

- **`register_agent`** — on an existing agent, drop `name` from the update set
  entirely; only `workstream` / `description` (when provided) and `updated_at`
  change. First registration still defaults `name` to the directory basename and
  accepts an explicit `name`.
- **`rename_agent(agent_cwd, name)` / `agent_rename`** — new function + tool that
  validates the name (non-blank, length-capped) and sets it for the resolved
  agent. `agent_cwd` is canonicalized in lookup mode (`require_exists=False`) so
  a moved repo's stored alias still resolves. Raises if the path is
  unregistered.
- **Docs** — `agent_register` / `agent_rename` docstrings, param descriptions,
  and `SKILL.md` state the sticky-name contract and that renames are explicit,
  user-driven actions.

## Consequences

### Positive

- Agent public handles stay stable across sessions, so `inbox_send`-by-name and
  the `agent_list` directory stay reliable.
- Re-registration at session start is now truly identity-preserving.
- Renaming is a deliberate, auditable action gated behind a distinct tool.

### Negative

- Passing `name` to `agent_register` for an existing agent is a no-op; a caller
  expecting the old rewrite behavior must switch to `agent_rename`.

### Neutral / forward-looking

- The server cannot enforce "only rename when the user asks" over stdio; that
  guardrail lives in the tool docs the model reads, same posture as the
  hard-delete confirmation in [ADR 0005](0005-note-soft-delete.md).
