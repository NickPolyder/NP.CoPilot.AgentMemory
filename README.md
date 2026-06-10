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
| MCP server (Python)                | `server/np_agent_memory/` ✅ Phase 1                                |
| Bundled skill                      | `skills/agent-memory/SKILL.md` ✅ Phase 8                           |
| Plugin manifest                    | `.claude-plugin/plugin.json` ✅ Phase 1                             |
| MCP registration                   | `.mcp.json` ✅ Phase 1                                              |
| Installer (venv + deps)            | `install.ps1` ✅ Phase 1                                            |
| Runtime SQLite DB                  | `$HOME\.copilot\np-agent-memory\agent-memory.db` *(plugin-owned, Phase 2)*   |
| Runtime backups                    | `$HOME\.copilot\np-agent-memory\backups\` *(Phase 7)*               |
| Runtime logs                       | `$HOME\.copilot\np-agent-memory\logs\`                              |

The runtime data folder uses the plugin name (`np-agent-memory`) so its
provenance is obvious to anyone inspecting `$HOME\.copilot\`.

## Where to start

1. Read [`docs/PLAN.md`](docs/PLAN.md) end-to-end.
2. Read [`docs/TASKS.md`](docs/TASKS.md) for the phased breakdown.
3. Phase 0 (the spike) is complete — see [`docs/spike-0.md`](docs/spike-0.md)
   and [`docs/decisions/0001-stdio-vs-long-lived-backend.md`](docs/decisions/0001-stdio-vs-long-lived-backend.md).

## Install (development loop)

Requires PowerShell 7+ and Python 3.12+ on PATH (or the Windows `py` launcher).
The migration runner depends on `sqlite3.connect(autocommit=True)` and
`datetime.UTC`, both of which require Python 3.12 or newer.

```powershell
# 1. Build the bundled venv and self-verify the server package imports.
./install.ps1

# 2. Register the marketplace and install the plugin.
#    (Run inside the Copilot CLI.)
/plugin marketplace add "C:\path\to\NP.CoPilot.AgentMemory"
/plugin install np-agent-memory@np-agent-memory-marketplace

# 3. Restart the CLI, then verify the server loaded by calling the tool:
#    np-agent-memory-memory_alive
```

Re-running `install.ps1` is idempotent. It will reuse an existing `.venv`,
re-pin dependencies from `requirements.txt`, and re-verify the import.

> **Pre-release note:** the bundled `0001_init.sql` is still being revised. The
> runner refuses to start if a previously-applied migration's checksum changed
> (it guards against edits to shipped SQL). Until v1 ships, if you hit a
> "checksum mismatch" error after pulling, delete your dev database at
> `$HOME\.copilot\np-agent-memory\` (or wherever `AGENT_MEMORY_DIR` points) and
> let it re-initialize.

## Development tooling

Dev dependencies (test runner + linter) are pinned in `requirements-dev.txt`:

```powershell
# Install dev tooling into the bundled venv.
./.venv/Scripts/python.exe -m pip install -r requirements-dev.txt

# Lint and format (Ruff — config in pyproject.toml).
./.venv/Scripts/ruff.exe check server          # lint
./.venv/Scripts/ruff.exe check --fix server    # lint + autofix
./.venv/Scripts/ruff.exe format server         # format (Black-compatible)

# Run the test suite.
$env:PYTHONPATH = "$(Get-Location)\server"
./.venv/Scripts/python.exe -m pytest server/tests/ -q
```

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
