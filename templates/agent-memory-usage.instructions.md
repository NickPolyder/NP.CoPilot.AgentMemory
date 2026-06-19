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
