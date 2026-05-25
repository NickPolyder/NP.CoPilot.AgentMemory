# NP.CoPilot.AgentMemory

A Copilot CLI plugin that gives every agent on Nick's machine a persistent,
shared memory across sessions — and a structured way to leave messages for
other agents.

> **Status:** Pre-implementation. The full design lives in [`docs/PLAN.md`](docs/PLAN.md).
> Implementation is owned by an agent working in this repository. The
> Connects agent (in `C:\path\to\Connects`) does NOT implement this —
> it only tracks progress.

## What this plugin does

- **Persistent memory per agent** — long-running todos, decisions, and notes
  survive across sessions. No more cold starts.
- **Cross-agent inbox** — agents can leave structured messages for other
  agents (keyed by working-tree path).
- **Handover transport** — replaces today's markdown handover files. Connects
  ingests directly from the shared SQLite DB via a crash-safe claim / ack
  protocol.
- **Human-readable on demand** — `memory_export` and `handover_export` tools
  regenerate markdown when you want to read or share it.

## Where things live

| Thing                              | Location                                                            |
|------------------------------------|---------------------------------------------------------------------|
| Plan / architecture                | `docs/PLAN.md`                                                      |
| MCP server (Python)                | `server/` *(not yet created — Phase 1+)*                            |
| Bundled skill                      | `skills/agent-memory/SKILL.md` *(not yet created — Phase 8)*        |
| Plugin manifest                    | `.claude-plugin/plugin.json` *(not yet created — Phase 1)*          |
| MCP registration                   | `.mcp.json` *(not yet created — Phase 1)*                           |
| Runtime SQLite DB                  | `$HOME\.copilot\np-agent-memory\agent-memory.db` *(plugin-owned)*   |
| Runtime backups                    | `$HOME\.copilot\np-agent-memory\backups\`                           |
| Runtime logs                       | `$HOME\.copilot\np-agent-memory\logs\`                              |

The runtime data folder uses the plugin name (`np-agent-memory`) so its
provenance is obvious to anyone inspecting `$HOME\.copilot\`.

## Where to start

1. Read [`docs/PLAN.md`](docs/PLAN.md) end-to-end.
2. Read [`docs/TASKS.md`](docs/TASKS.md) for the phased breakdown.
3. Phase 0 (the spike) must pass before any real code is written. See
   the "Implementation phases" section of the plan.

## Conventions

- **C# / PowerShell / Python style** follow the conventions in
  `C:\path\to\NP.CoPilot.Config\instructions\` (auto-loaded by the
  Copilot CLI when working in this repo).
- **Commits** use Conventional Commits, imperative mood, and include the
  `Co-authored-by: Copilot` trailer.
- **Markdown** in `docs/` is the source of truth for design. Update it
  whenever an implementation decision diverges from the plan.

## License

MIT (TBD — finalize before publishing).
