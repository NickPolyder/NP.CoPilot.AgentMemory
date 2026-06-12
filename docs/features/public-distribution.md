# Public distribution — install straight from GitHub

Status: **done ✅** (v0.4.0). Makes the plugin installable by anyone from this
repo with no clone and no manual setup, by adding a self-bootstrapping launcher
and hardening the manifests/docs for a public release. Decision rationale is in
[ADR 0003](../decisions/0003-public-distribution-bootstrap.md).

## Problem

The plugin's `.mcp.json` pointed at a repo-local `${PLUGIN_ROOT}/.venv` that
never ships (gitignored; a venv isn't portable across OS/arch/Python-version).
The Copilot CLI runs **no** install/build hook, and a misconfigured stdio
server fails **silently** to the agent (only stderr in
`~/.copilot/logs/process-*.log`). So a remote `/plugin install` produced a
dead server.

## Solution: self-bootstrapping launcher

`.mcp.json` now launches `py -3 ${PLUGIN_ROOT}/bootstrap.py`. The stdlib-only
[`bootstrap.py`](../../bootstrap.py):

1. Resolves the runtime dir (`AGENT_MEMORY_DIR` or
   `$HOME/.copilot/np-agent-memory`) — mirrors `db.get_data_dir` (can't import
   the package before the venv exists).
2. Guards Python ≥ 3.12; on failure logs a loud `FATAL` line and exits non-zero.
3. Considers the venv ready only when its interpreter exists **and** a marker's
   `requirements.txt` sha256 matches (so a deps change forces a rebuild).
4. Cold/stale path: builds into `<venv>.tmp-<pid>`, `pip install`s pinned deps,
   writes the marker, then atomically `os.replace`s into place — under a
   directory lock so concurrent CLI windows can't race-corrupt the venv. Reuses
   the backup-R8 temp+rename pattern.
5. Warm path: skips straight to exec.
6. Execs `python -m np_agent_memory` with **inherited stdio**, forwarding the
   exit code.

The runtime venv lives at `$HOME\.copilot\np-agent-memory\.venv`, in the
runtime dir — so it **survives `copilot plugin update`** (the install dir may
be replaced).

## What shipped

| Change | File(s) |
|---|---|
| Launcher | `bootstrap.py` |
| MCP wiring | `.mcp.json` (`py -3 bootstrap.py`) |
| Launcher tests | `server/tests/test_bootstrap.py` (25 tests) |
| Public manifest | `.claude-plugin/plugin.json` (+license/homepage/repository, generic description) |
| Marketplace | `.claude-plugin/marketplace.json` (generic description) |
| License | `LICENSE` (MIT) |
| Version align | `0.4.0` across `plugin.json`, `pyproject.toml`, `__init__.py` |
| Install docs | `README.md` "Install (just use it)" + forward-only schema note |
| ADR | `docs/decisions/0003-public-distribution-bootstrap.md` |

## Prerequisite

**Python 3.12+ on PATH** (`py -3` must resolve to ≥ 3.12). The server uses
`sqlite3.connect(autocommit=True)` and `datetime.UTC`, both 3.12+.

## Validation

- **Unit:** 25 launcher tests (runtime-dir resolution, marker freshness, atomic
  swap + temp/old cleanup, failure leaves prior venv intact, lock acquire /
  stale-reclaim / wait-timeout, exec env/PYTHONPATH/exit-code, `--ensure-only`
  pre-warm). Full suite: 261 passing, ruff clean.
- **Clean-machine cold build:** throwaway `AGENT_MEMORY_DIR`, only system
  Python 3.14.6 on PATH → bootstrap built the venv, server started under the
  runtime venv interpreter, DB created, `0001_init.sql` applied, exit 0.
- **Warm path:** re-run skipped the build (no "building runtime venv" log),
  ~1.1s startup.

## Schema is forward-only

`0001_init.sql` is **frozen** (the migration runner rejects checksum changes to
applied migrations). Any schema change ships as a new `0002_*.sql` etc. The
README's old "pre-release / delete your DB" caveat was rewritten to describe the
forward-only model (with a pre-1.0 early-adopter escape hatch).

## Roadmap — cross-platform launcher (Linux/macOS)

Out of scope for v0.4.0 (Windows-first). Tracked for a later step:

- `.mcp.json` is Windows-specific (`py -3`). Linux/macOS need `python3` and the
  `bin/` venv layout. Options: per-OS `.mcp.json` if the CLI supports it, or a
  thin OS-detecting shim.
- `bootstrap.py` already resolves the interpreter path per-OS
  (`venv_python` handles `Scripts/` vs `bin/`) and uses `os.name` — the
  remaining gap is the **entry command** in `.mcp.json`.
- Revisit PyPI/`uvx` as an alternative runtime provisioner when going
  cross-platform (see ADR 0003, Option B).
- A bundled standalone runtime (PyInstaller) stays a fallback if the
  Python-on-PATH prerequisite proves painful.

## Follow-ups / risks

- **Python-on-PATH dependency** — if `py -3` resolves to < 3.12 (or Python is
  absent), the server won't start; it logs the reason. Documented in the README
  prereq.
- **First-session cold-build latency** could look like "tools missing" until the
  build completes. Mitigated by `install.ps1` pre-warm and clear logging;
  revisit if it bites in real use.
