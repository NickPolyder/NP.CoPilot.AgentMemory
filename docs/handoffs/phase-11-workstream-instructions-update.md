# Phase 11 — Add a global "Use agent-memory" instruction

### 🔄 Handoff: np-agent-memory implementing agent → user (machine config)

**Reason:** Every agent on this machine should register with `np-agent-memory`
at session start and use it for durable memory, todos, blockers, the cross-agent
inbox, and handovers. Rather than copy that habit into every repo's
`copilot-instructions.md`, install it **once** as a global instruction that
auto-applies to all repos.

**Priority:** Advisory — apply once, machine-wide.

---

## Context

- The `np-agent-memory` plugin is installed machine-wide and ships the
  [`agent-memory` skill](../../skills/agent-memory/SKILL.md), which is the full
  reference. This global instruction just makes each agent *opt in* by default
  and links to that skill.
- Tools are namespaced `np-agent-memory-<tool>`. Every agent-scoped tool needs
  `agent_cwd` = the canonical repo root.
- **Why global instead of per-repo:** a single file at
  `~/.copilot/instructions/` is loaded for every repo, so there is one place to
  edit and no per-repo drift. The trade-off is that it lives on **this machine
  only** — it does not travel to other people who install the plugin from the
  marketplace. Only the bundled skill travels. For a single-machine,
  multi-agent setup this is exactly the behavior we want.

## Request

Create the global instruction file at:

```text
~/.copilot/instructions/agent-memory-usage.instructions.md
```

with the following content:

```markdown
---
applyTo: '**'
---

# Use agent-memory (np-agent-memory plugin)

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
  name or canonical path); ack what you receive. Use
  `np-agent-memory-agent_list` to discover which peers are registered.

At session end, save a handover with `np-agent-memory-handover_save` (or run the
`handover-report` skill). Do not write handover markdown files.

See the bundled **agent-memory** skill for the full tool reference and the
`agent_cwd` contract.
```

A versioned copy of this file is shipped in the repo at
[`templates/agent-memory-usage.instructions.md`](../../templates/agent-memory-usage.instructions.md)
so it stays under source control; copy it into `~/.copilot/instructions/`
(optionally via `install.ps1`).

## Constraints / locked decisions

- Always pass `agent_cwd` = canonical repo root; never invent a path (it mints a
  duplicate identity). Project config extends global config — don't contradict
  it.
- Keep it short; the bundled skill is the source of truth for detail. Link to
  it rather than duplicating the full tool list.
- `applyTo: '**'` makes the instruction always-active across every repo. Do not
  narrow it unless you intend to scope agent-memory to specific paths.

## Done when

- `~/.copilot/instructions/agent-memory-usage.instructions.md` exists and is
  loaded for every repo (no per-repo `copilot-instructions.md` edits required).
- A fresh session in any repo registers the agent and reads its inbox/timeline
  on turn 1.
