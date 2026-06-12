# NP.CoPilot.AgentMemory

A Copilot CLI plugin that gives every agent on your machine a persistent,
shared memory across sessions — and a structured way to leave messages for
other agents.

> **Status:** v0.4.0 — usable and under real-world shakeout. Installs straight
> from this repo (see [Install](#install-just-use-it) below). The full design
> lives in [`docs/PLAN.md`](docs/PLAN.md).

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
| Runtime launcher (builds venv)     | `bootstrap.py` *(self-bootstrapping, Phase R+)*                    |
| Dev installer (venv + deps)        | `install.ps1` ✅ Phase 1                                            |
| Runtime Python venv                | `$HOME\.copilot\np-agent-memory\.venv\` *(built on first launch)*  |
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

## Install (just use it)

You do **not** need to clone this repo. The only prerequisite is **Python
3.12+ on PATH** (the Windows `py -3` launcher must resolve to 3.12 or newer).

Run these inside the Copilot CLI:

```text
# Option A — install directly from the repo.
/plugin install NickPolyder/NP.CoPilot.AgentMemory

# Option B — register it as a marketplace first, then install by name.
/plugin marketplace add NickPolyder/NP.CoPilot.AgentMemory
/plugin install np-agent-memory@np-agent-memory-marketplace
```

Restart the CLI. On the **first** launch the plugin builds its own Python
runtime (a virtualenv with the pinned dependencies) under
`$HOME\.copilot\np-agent-memory\.venv`; this takes a few tens of seconds and
its progress is logged to `~/.copilot/logs/process-*.log`. Every launch after
that is instant. Verify the server is up by calling the tool:

```text
np-agent-memory-memory_alive
```

The runtime venv, database, and backups live in
`$HOME\.copilot\np-agent-memory\` (override with `AGENT_MEMORY_DIR`) and
**survive `copilot plugin update`** — only the install directory is replaced on
update.

> **No Python 3.12+?** The server cannot start and logs a clear `FATAL` line to
> the process log. Install a newer Python (make sure `py -3` resolves to it)
> and restart the CLI.

## Install (development loop)

If you are *contributing* to the plugin, use the local installer to pre-build a
repo-local `.venv` and self-verify the package imports. Requires PowerShell 7+
and Python 3.12+ on PATH.

```powershell
# 1. Build the bundled venv and self-verify the server package imports.
#    Add -PrewarmRuntime to also build the runtime venv now (instant first
#    session) instead of letting bootstrap.py build it on first launch.
./install.ps1            # dev venv only
./install.ps1 -PrewarmRuntime   # dev venv + pre-built runtime venv

# 2. Register the local checkout as a marketplace and install the plugin.
#    (Run inside the Copilot CLI.)
/plugin marketplace add "H:\Repos\NP\NP.CoPilot.AgentMemory"
/plugin install np-agent-memory@np-agent-memory-marketplace

# 3. Restart the CLI, then verify the server loaded by calling the tool:
#    np-agent-memory-memory_alive
```

Re-running `install.ps1` is idempotent. It will reuse an existing `.venv`,
re-pin dependencies from `requirements.txt`, and re-verify the import. The
repo-local `.venv` is a dev convenience; at runtime the plugin always uses the
self-bootstrapped venv in the runtime data dir (built by `bootstrap.py`).

> **Schema is forward-only.** The bundled `0001_init.sql` is frozen: the
> migration runner refuses to start if a previously-applied migration's
> checksum changes, so shipped SQL is never edited in place. Any schema change
> ships as a **new** migration (`0002_*.sql`, `0003_*.sql`, …) that the runner
> applies on next launch. (During the pre-1.0 shakeout, if you are an early
> adopter and a not-yet-released `0001` changes underneath you, delete your dev
> database at `$HOME\.copilot\np-agent-memory\` and let it re-initialize.)

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

MIT — see [`LICENSE`](LICENSE).
