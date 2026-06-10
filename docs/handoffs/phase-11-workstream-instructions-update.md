# Phase 11 — Add a "Use agent-memory" section to each workstream's instructions

### 🔄 Handoff: np-agent-memory implementing agent → each workstream agent

**Reason:** Every workstream agent should register with `np-agent-memory` at
session start and use it for durable memory, todos, blockers, the cross-agent
inbox, and handovers. That habit lives in each repo's
`copilot-instructions.md` (or `.github/copilot-instructions.md`), which only the
owning workstream agent should edit.

**Priority:** Advisory — apply per repo as each workstream is touched.

---

## Context

- The `np-agent-memory` plugin is installed machine-wide and ships the
  [`agent-memory` skill](../../skills/agent-memory/SKILL.md), which is the full
  reference. This section just makes each agent *opt in* by default.
- Tools are namespaced `np-agent-memory-<tool>`. Every agent-scoped tool needs
  `agent_cwd` = the canonical repo root.

## Request

Add the following section to each workstream's `copilot-instructions.md`
(adjust the `name` / `workstream` to fit that agent):

```markdown
## Use agent-memory (np-agent-memory plugin)

On your **first turn** each session, register and orient yourself:

1. Resolve your canonical root: `git rev-parse --show-toplevel`.
2. `np-agent-memory-agent_register(name="<this-agent>", agent_cwd=<root>,
   workstream="<workstream>")` — idempotent; safe every session.
3. `np-agent-memory-agent_describe(agent_cwd)` for open todos / blockers /
   unread messages, `np-agent-memory-memory_query(agent_cwd, limit=20)` for your
   recent timeline, and `np-agent-memory-inbox_check(agent_cwd, limit=20)` for
   messages from other agents.

During the session:

- **Log decisions and non-obvious progress** with
  `np-agent-memory-memory_log` (`category` ∈ `progress`/`decision`/`note`).
  Skip routine/obvious chatter.
- **Track work that outlives the session** with `np-agent-memory-todo_*`.
- **Record real impediments** with `np-agent-memory-blocker_*`.
- **Coordinate** by sending `np-agent-memory-inbox_send` to another agent (by
  name or canonical path); ack what you receive.

At session end, save a handover with `np-agent-memory-handover_save` (or run the
`handover-report` skill). Do not write handover markdown files.

See the bundled **agent-memory** skill for the full tool reference and the
`agent_cwd` contract.
```

## Constraints / locked decisions

- Always pass `agent_cwd` = canonical repo root; never invent a path (it mints a
  duplicate identity). Project config extends global config — don't contradict
  it.
- Keep it short; the bundled skill is the source of truth for detail. Link to
  it rather than duplicating the full tool list.

## Done when

- Each active workstream's `copilot-instructions.md` has a "Use agent-memory"
  section.
- A fresh session in that repo registers the agent and reads its inbox/timeline
  on turn 1.
