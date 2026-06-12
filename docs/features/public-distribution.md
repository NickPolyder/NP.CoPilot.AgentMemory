# Public distribution — install straight from GitHub

Status: **done ✅** (v0.5.0). Makes the plugin installable by anyone from this
repo with no clone and no manual setup, by launching the server through
[`uvx`](https://docs.astral.sh/uv/) and hardening the manifests/docs for a
public release. Decision rationale is in
[ADR 0004](../decisions/0004-launcher-via-uvx.md) (which supersedes the
bootstrap launcher in [ADR 0003](../decisions/0003-public-distribution-bootstrap.md)).

## Problem

The plugin's `.mcp.json` originally pointed at a repo-local `${PLUGIN_ROOT}/.venv`
that never ships (gitignored; a venv isn't portable across OS/arch/Python-version).
The Copilot CLI runs **no** install/build hook, and a misconfigured stdio server
fails **silently** to the agent (only stderr in `~/.copilot/logs/process-*.log`).
So a remote `/plugin install` produced a dead server.

v0.4.0 fixed this with a stdlib-only `bootstrap.py` that built a venv on first
launch — but it was **Windows-only** (`py -3`), reimplemented a package manager
(~225 lines + 25 tests of venv build, staleness marker, atomic swap, lockfile),
and still depended on the right system Python being first on `PATH`.

## Solution: launch via `uvx`

`.mcp.json` now launches `uvx --from ${PLUGIN_ROOT} np-agent-memory`. `uv` builds
the installed plugin into its own managed cache, resolves the pinned deps, and
runs the `np-agent-memory` console script — **provisioning a Python ≥ 3.12 itself**
if the machine doesn't have one. The same command works on Windows, Linux, and
macOS, so cross-platform support falls out for free.

This required making the project a real installable package:

1. `[build-system]` = hatchling.
2. Runtime `dependencies` (`mcp==1.26.0`, `python-ulid==3.1.0`) and a `dev`
   optional-deps extra (`pytest`, `ruff`) — replacing `requirements*.txt`.
3. A console-script entry point: `np-agent-memory = "np_agent_memory.__main__:main"`.
4. `[tool.hatch.build.targets.wheel] packages = ["server/np_agent_memory"]` so
   the package (including `migrations/*.sql`) ships in the wheel.
5. `[tool.uv] cache-keys` watching `pyproject.toml` + the packaged source so a
   `copilot plugin update` is always picked up (uv's default only rebuilds a
   path build on a version change).

Runtime data (DB, backups, logs) still lives **outside** the install dir at
`$HOME\.copilot\np-agent-memory\` (`AGENT_MEMORY_DIR`), so it survives
`copilot plugin update` unchanged.

## What shipped

| Change | File(s) |
|---|---|
| Packaging | `pyproject.toml` (build-system, deps, dev extra, entry point, wheel mapping, `cache-keys`) |
| MCP wiring | `.mcp.json` (`uvx --from ${PLUGIN_ROOT} np-agent-memory`) |
| Removed launcher | `bootstrap.py`, `server/tests/test_bootstrap.py`, `requirements.txt`, `requirements-dev.txt` |
| Dev installer | `install.ps1` (now editable `pip install -e ".[dev]"` + self-verify) |
| Version align | `0.5.0` across `plugin.json`, `pyproject.toml`, `__init__.py` |
| Install docs | `README.md` "Install (just use it)" (uv prereq + uvx mechanism) |
| ADR | `docs/decisions/0004-launcher-via-uvx.md` (supersedes 0003) |

## Prerequisite

**`uv` on PATH.** Install with:

- Windows: `irm https://astral.sh/uv/install.ps1 | iex`
- Linux/macOS: `curl -LsSf https://astral.sh/uv/install.sh | sh`

`uv` provisions a matching Python ≥ 3.12 on its own (honoring `requires-python`),
so there is no separate "Python on PATH" requirement. The server uses
`sqlite3.connect(autocommit=True)` and `datetime.UTC`, both 3.12+.

## Validation

- **Unit:** full suite **236 passing**, ruff clean (the 25 bootstrap tests were
  removed with the launcher).
- **Wheel contents:** built wheel inspected — `np_agent_memory/migrations/0001_init.sql`
  is present, so migrations ship (the runner discovers `.sql` via
  `Path(__file__).parent.iterdir()`, which works from a real install dir).
- **Cold start:** `uvx --from . np-agent-memory` with a throwaway
  `AGENT_MEMORY_DIR` built the project, installed 33 packages, booted the
  server, created the DB, applied `0001_init.sql`, exit 0.
- **Warm path:** re-run reused the uv cache in ~1.1 s (no rebuild).
- **Update detection:** touching a source file's mtime (no version bump) forced
  a rebuild on the next `uvx` launch — confirming `cache-keys` picks up plugin
  updates.

## Schema is forward-only

`0001_init.sql` is **frozen** (the migration runner rejects checksum changes to
applied migrations). Any schema change ships as a new `0002_*.sql` etc. The
README's old "pre-release / delete your DB" caveat describes the forward-only
model (with a pre-1.0 early-adopter escape hatch).

## Follow-ups / risks

- **`uv` prerequisite** — if `uv` isn't on PATH the server won't start; the CLI
  surfaces the spawn failure in `~/.copilot/logs/process-*.log`. Documented in
  the README prereq.
- **First-session cold-build latency** (uv resolve + install, tens of seconds)
  could look like "tools missing" until it completes; warm launches are ~1.1 s.
- **PyPI option (post-1.0):** publishing the package and switching to
  `uvx np-agent-memory` (no `--from`) is the cleanest consumer story once the
  tool API stabilizes. See ADR 0004, Option C.
