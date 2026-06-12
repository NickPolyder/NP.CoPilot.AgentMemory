# ADR 0004 — Launch the server via `uvx` instead of a bootstrap launcher

- **Status:** Accepted
- **Date:** 2026-06-11
- **Decider:** Nick Polyderopoulos
- **Supersedes:** [ADR 0003](0003-public-distribution-bootstrap.md)
- **Related:** [`docs/PLAN.md`](../PLAN.md), [`docs/spike-0.md`](../spike-0.md),
  [`docs/features/public-distribution.md`](../features/public-distribution.md),
  [`pyproject.toml`](../../pyproject.toml), [`.mcp.json`](../../.mcp.json),
  [`install.ps1`](../../install.ps1)

## Context

[ADR 0003](0003-public-distribution-bootstrap.md) chose a stdlib-only
`bootstrap.py` that builds a runtime venv on first launch. It shipped in v0.4.0
and worked, but it carried two structural problems:

1. **Windows-only.** `.mcp.json` ran `py -3 bootstrap.py`. The `py` launcher
   does not exist on Linux/macOS, so cross-platform support was an explicit,
   unstarted roadmap item — the launcher itself would have to grow per-OS
   branches plus a per-OS `.mcp.json`.
2. **It reimplemented a package manager.** `bootstrap.py` hand-rolled venv
   creation, a `requirements.txt`-hash staleness marker, a temp-build +
   `os.replace` atomic swap, and a cross-process lock — ~200 lines (plus 25
   tests) of provisioning logic that mature tooling already solves.

It also still depended on **a correctly-versioned system Python being first on
`PATH`** (`py -3` resolving to ≥ 3.12), which is the fragile part of any
"bring your own Python" scheme.

Since 0003 was written, `uv`/`uvx` (Astral) matured into the de-facto standard
for exactly this job: a single self-contained binary that resolves a project,
builds it into a managed cache, **and can auto-provision a matching Python**
honoring `requires-python`. Making the project a real installable package
(needed for `uvx`) is independently desirable.

## Options considered

### Option A — `uvx --from ${PLUGIN_ROOT}` (chosen)

Make the project a proper hatchling-built package and point `.mcp.json` at
`uvx --from ${PLUGIN_ROOT} np-agent-memory`. `uv` builds the plugin from its
installed directory into its own cache, resolves the pinned deps, and runs the
`np-agent-memory` console script — provisioning Python 3.12+ itself if needed.

- **Pros**
  - **One `.mcp.json` for every OS** — `uvx` is the command on Windows, Linux,
    and macOS. Cross-platform falls out for free.
  - `uv` **auto-provisions a Python ≥ 3.12** (per `requires-python`), removing
    the "correct system Python on PATH" dependency that was the weakest point
    of the bootstrap approach.
  - Deletes all hand-rolled provisioning: no venv build, staleness marker,
    atomic swap, or lockfile to own and test (~225 lines + 25 tests removed).
  - `${PLUGIN_ROOT}` keeps the **repo as the single source of truth** — no PyPI
    release coupling (unlike 0003's Option B). The plugin dir *is* the source.
  - Runtime data (DB, backups, logs) still lives outside the install dir
    (`AGENT_MEMORY_DIR`), unchanged and untouched by updates.
- **Cons**
  - Swaps the prerequisite from "Python 3.12+ on PATH" to "**`uv` on PATH**".
    Still one prerequisite, but a single, self-updating binary that provisions
    its own Python — strictly less fragile.
  - First launch does a cold build (resolve + install ~33 packages, tens of
    seconds); warm launches reuse the uv cache (~1.1 s).
  - `uvx --from <local path>` cache invalidation: by default `uv` only rebuilds
    a path build when the **version** changes. Mitigated below.

### Option B — Keep the `bootstrap.py` launcher (status quo, ADR 0003)

- **Pros**: already shipped; no new prerequisite beyond Python.
- **Cons**: Windows-only without further per-OS work; we own and test a
  package-manager reimplementation; still depends on the right system Python.

### Option C — Publish to PyPI, launch via `uvx np-agent-memory`

- **Pros**: cleanest consumer story (`uvx <name>`); no `--from`.
- **Cons**: couples every plugin update to a PyPI release and a publishing
  pipeline; the repo stops being the single source of truth. Premature pre-1.0.
  (Kept as a future step once the API stabilizes.)

## Decision

**Adopt Option A — `uvx --from ${PLUGIN_ROOT} np-agent-memory`.** This
supersedes the bootstrap launcher from ADR 0003 and resolves cross-platform
support in the same move (the Linux/macOS roadmap item from 0003 is closed by
this decision, not deferred).

### Implementation

- **`pyproject.toml`** — add a `[build-system]` (hatchling); declare runtime
  `dependencies` (`mcp==1.26.0`, `python-ulid==3.1.0`); a `dev` optional-deps
  extra (`pytest`, `ruff`, replacing `requirements*.txt`); a console-script
  entry point `np-agent-memory = "np_agent_memory.__main__:main"`; and
  `[tool.hatch.build.targets.wheel] packages = ["server/np_agent_memory"]` so
  the package (including `migrations/*.sql`) ships in the wheel.
- **`[tool.uv] cache-keys`** — watch `pyproject.toml` and the packaged source
  (`server/np_agent_memory/**/*.py`, `**/*.sql`) so a `copilot plugin update`
  is always picked up, even if a release forgets to bump the version (covers
  the path-cache-invalidation caveat above).
- **`.mcp.json`** — `command: "uvx"`,
  `args: ["--from", "${PLUGIN_ROOT}", "np-agent-memory"]`, env only
  `PYTHONUNBUFFERED=1` (the `PYTHONPATH` shim is gone — the package is
  installed, not run from source).
- **Remove** `bootstrap.py`, `server/tests/test_bootstrap.py`,
  `requirements.txt`, `requirements-dev.txt`.
- **`install.ps1`** — repurposed as a **dev-only** editable installer
  (`pip install -e ".[dev]"` + self-verify). It is not part of the consumer
  runtime path; `uvx` is.

## Validation

- `install.ps1` runs an editable install and self-verifies at v0.5.0.
- Full suite green: **236 tests pass** (25 bootstrap tests removed with the
  launcher).
- Built wheel inspected: `np_agent_memory/migrations/0001_init.sql` **is
  present** — migrations ship correctly (the runner discovers `.sql` via
  `Path(__file__).parent.iterdir()`, which works from a real install dir).
- `uvx --from . np-agent-memory` **cold start**: built the project, installed
  33 packages, booted the server, created the DB, applied `0001_init.sql`,
  exit 0.
- **Warm** run reused the uv cache in ~1.1 s (no rebuild).
- **`cache-keys`**: touching a source file's mtime (no version bump) forced a
  rebuild on the next `uvx` launch — confirming plugin updates are picked up.

## Consequences

### Positive

- A single `.mcp.json` works on Windows, Linux, and macOS — cross-platform is
  solved, not deferred.
- No more hand-rolled provisioning to maintain or test; `uv` owns venv, deps,
  and Python provisioning.
- The repo remains the single source of truth (`--from ${PLUGIN_ROOT}`), with
  no PyPI release coupling.
- Runtime DB/backups/logs are unaffected by updates (unchanged from 0003).

### Negative

- New prerequisite: **`uv` on PATH** (`irm https://astral.sh/uv/install.ps1 |
  iex` on Windows; `curl -LsSf https://astral.sh/uv/install.sh | sh`
  elsewhere). Documented in the README.
- First-session cold build latency remains (now uv's resolve+install rather
  than our venv build).

### Neutral / forward-looking

- Publishing to PyPI and switching to `uvx np-agent-memory` (Option C) stays a
  post-1.0 option once the tool API stabilizes.
