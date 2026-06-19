# NP.CoPilot.AgentMemory

A Copilot CLI plugin that gives every agent on your machine a persistent,
shared memory across sessions — and a structured way to leave messages for
other agents.

> **Status:** v0.7.0 — usable and under real-world shakeout. Installs straight
> from this repo and runs via [`uv`](https://docs.astral.sh/uv/) (see
> [Install](#install-just-use-it) below). The full design lives in
> [`docs/PLAN.md`](docs/PLAN.md).

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
| Dev installer (editable venv)      | `install.ps1` ✅ Phase 1                                            |
| Runtime Python env                 | uv-managed (built from the plugin via `uvx`; uv cache)             |
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

You do **not** need to clone this repo. The only prerequisite is
[**`uv`**](https://docs.astral.sh/uv/) on PATH — uv builds and runs the server,
and can even provision a compatible Python 3.12+ itself if you don't have one.

```powershell
# Install uv once (Windows). See https://docs.astral.sh/uv/ for other OSes.
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# Install uv once (Linux/macOS).
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then, inside the Copilot CLI:

```text
# Option A — install directly from the repo.
/plugin install NickPolyder/NP.CoPilot.AgentMemory

# Option B — register it as a marketplace first, then install by name.
/plugin marketplace add NickPolyder/NP.CoPilot.AgentMemory
/plugin install np-agent-memory@np-agent-memory-marketplace
```

Restart the CLI. `.mcp.json` launches the server as
`uvx --from ${PLUGIN_ROOT} np-agent-memory`: on the **first** launch uv builds
the project and resolves its pinned dependencies (a few seconds, logged to
`~/.copilot/logs/process-*.log`); subsequent launches reuse uv's cache. Verify
the server is up by calling the tool:

```text
np-agent-memory-memory_alive
```

The database, backups, and logs live in `$HOME\.copilot\np-agent-memory\`
(override with `AGENT_MEMORY_DIR`) and are independent of the plugin install
directory, so they **survive `copilot plugin update`**.

> **No `uv`?** The server cannot start; the CLI logs the failure to
> `~/.copilot/logs/process-*.log`. Install uv (command above) and restart the
> CLI. uv handles the Python runtime — you do **not** need a separate Python
> 3.12+ on PATH.

## Install (development loop)

If you are *contributing*, use the local installer to build a repo-local `.venv`
with the project installed editable (`pip install -e ".[dev]"`) and self-verify
the package imports. Requires PowerShell 7+ and Python 3.12+ on PATH.

```powershell
# 1. Build the dev venv (editable install + dev extras) and self-verify imports.
./install.ps1

# 2. Register the local checkout as a marketplace and install the plugin.
#    (Run inside the Copilot CLI; the plugin itself still launches via uvx.)
/plugin marketplace add "H:\Repos\NP\NP.CoPilot.AgentMemory"
/plugin install np-agent-memory@np-agent-memory-marketplace

# 3. Restart the CLI, then verify the server loaded by calling the tool:
#    np-agent-memory-memory_alive
```

Re-running `install.ps1` is idempotent. The repo-local `.venv` is a dev
convenience (tests, linting); at runtime the plugin always runs via `uvx` from
the installed plugin directory.

> **Schema is forward-only.** The bundled `0001_init.sql` is frozen: the
> migration runner refuses to start if a previously-applied migration's
> checksum changes, so shipped SQL is never edited in place. Any schema change
> ships as a **new** migration (`0002_*.sql`, `0003_*.sql`, …) that the runner
> applies on next launch. (During the pre-1.0 shakeout, if you are an early
> adopter and a not-yet-released `0001` changes underneath you, delete your dev
> database at `$HOME\.copilot\np-agent-memory\` and let it re-initialize.)

## Development tooling

Dev dependencies (test runner + linter) are declared as the `dev` extra in
`pyproject.toml` and installed by `install.ps1`:

```powershell
# (install.ps1 already ran `pip install -e ".[dev]"`.)

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
