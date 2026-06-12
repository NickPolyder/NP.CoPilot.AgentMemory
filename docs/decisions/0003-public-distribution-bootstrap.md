# ADR 0003 — Public distribution via a self-bootstrapping launcher

- **Status:** Superseded by [ADR 0004](0004-launcher-via-uvx.md)
- **Date:** 2026-06-11
- **Decider:** Nick Polyderopoulos
- **Context window:** Phase R closeout, first public release prep (v0.4.0)
- **Related:** [`docs/PLAN.md`](../PLAN.md), [`docs/spike-0.md`](../spike-0.md),
  [`docs/features/public-distribution.md`](../features/public-distribution.md),
  [`bootstrap.py`](../../bootstrap.py), [`.mcp.json`](../../.mcp.json),
  [`install.ps1`](../../install.ps1)

## Context

We want anyone — not just a contributor with the repo cloned — to install the
plugin straight from GitHub:

```text
/plugin install NickPolyder/NP.CoPilot.AgentMemory
```

The Copilot CLI copies the whole plugin folder into its install dir and wires
the MCP server from [`.mcp.json`](../../.mcp.json). Three hard facts (verified
in [`docs/spike-0.md`](../spike-0.md) against CLI 1.0.61) shape the problem:

1. **No install/build hook.** Plugin components are agents, skills, hooks,
   MCP/LSP servers. The CLI runs **no** post-install script. So nothing can
   "set up" the Python runtime at install time.
2. **A venv cannot be shipped prebuilt.** `.mcp.json` previously pointed at
   `${PLUGIN_ROOT}/.venv/Scripts/python.exe`, but that venv is gitignored and a
   venv is not portable across OS/arch/Python-version (native wheels such as
   `pydantic-core`). A consumer would get a silently broken server.
3. **A misconfigured stdio server fails silently to the agent.** The only
   evidence is stderr in `~/.copilot/logs/process-*.log`. So whatever provisions
   the runtime must be **loud** in that log.

The server itself needs a real Python ≥ 3.12 with the pinned deps
(`mcp`, `python-ulid`). The question is *how that runtime comes to exist on a
consumer's machine with zero manual steps*.

## Options considered

### Option A — Self-bootstrapping launcher (chosen)

`.mcp.json` runs a tiny **stdlib-only** [`bootstrap.py`](../../bootstrap.py)
under the system Python (`py -3`). On first launch it builds a venv with the
pinned deps in the runtime data dir, then execs `python -m np_agent_memory` over
the same stdio pipes.

- **Pros**
  - Zero manual steps for the consumer; works from a bare `/plugin install`.
  - The venv is built **for the consumer's** OS/arch/Python — no wheel
    portability problem.
  - Runtime venv lives in the runtime data dir
    (`$HOME\.copilot\np-agent-memory\.venv`), so it **survives
    `copilot plugin update`** (the install dir may be wiped).
  - Stdlib-only launcher has no chicken-and-egg dependency.
  - Reuses the proven temp-build + `os.replace` atomic-swap pattern (backup R8)
    and a cross-process lock, so concurrent CLI windows can't corrupt the venv.
- **Cons**
  - Requires **Python 3.12+ on PATH** (`py -3`) on the consumer's machine.
  - First-launch cold build (venv + pip) takes tens of seconds; tools may look
    "missing" until it finishes. Mitigated by pre-warm (`install.ps1`) and clear
    logging.
  - One extra wrapper process per session (the bootstrap parent stays alive and
    forwards the exit code).

### Option B — Publish to PyPI and launch via `uvx` / `pipx`

`.mcp.json` runs `uvx np-agent-memory` (or `pipx run`).

- **Pros**
  - Offloads venv/dep management to a mature tool; clean updates.
- **Cons**
  - Adds a **second** hard prerequisite (`uv`/`pipx`) on top of Python, and a
    publishing pipeline + release cadence we don't want yet.
  - Couples every plugin update to a PyPI release; the repo is no longer the
    single source of truth.
  - Premature for a pre-1.0 plugin under active shakeout.

### Option C — Bundle a standalone runtime (PyInstaller / embeddable Python)

Ship a frozen binary so there's no Python prerequisite at all.

- **Pros**
  - No Python-on-PATH dependency.
- **Cons**
  - Per-OS/arch build matrix and large binaries in the repo (or a release-asset
    fetch step the CLI won't run for us).
  - Heavy tooling for a plugin whose server is a few hundred lines of Python.
  - Out of proportion to the v0.4.0 goal.

## Decision

**Adopt Option A — a self-bootstrapping launcher.** Windows-first now; a
cross-platform launcher is an explicit later roadmap step
([`docs/TASKS.md`](../TASKS.md)).

### Implementation direction

- **`.mcp.json`**: `command: "py"`, `args: ["-3", "${PLUGIN_ROOT}/bootstrap.py"]`,
  keeping `PYTHONPATH=${PLUGIN_ROOT}/server` and `PYTHONUNBUFFERED=1`.
- **`bootstrap.py`** (stdlib only):
  1. Resolve the runtime dir (mirrors `db.get_data_dir`: `AGENT_MEMORY_DIR` or
     `$HOME/.copilot/np-agent-memory`).
  2. Guard Python ≥ 3.12; on failure print a `FATAL` line to stderr and exit
     non-zero (loud in the process log).
  3. Treat the venv as ready only when its interpreter exists **and** a marker
     file's `requirements.txt` sha256 matches — so a `requirements.txt` change
     triggers a rebuild.
  4. Cold/stale path: build into `<venv>.tmp-<pid>`, `pip install -r
     requirements.txt`, write the marker, then atomically `os.replace` into
     place — guarded by a directory lock so concurrent launches don't race.
  5. Warm path: skip straight to exec.
  6. Exec `python -m np_agent_memory` with inherited stdio; forward the exit
     code.
- **`install.ps1`** stays as a dev tool and an **optional pre-warm** for the
  runtime venv (makes a contributor's first session instant). It is never
  required at runtime.

## Consequences

### Positive

- A bare `/plugin install owner/repo` yields a working server with no manual
  setup, on a fresh machine with only system Python.
- Plugin updates don't disturb the runtime venv, DB, or backups.
- Validated end-to-end: a clean-machine cold build (throwaway
  `AGENT_MEMORY_DIR`, system Python 3.14) built the venv, started the server,
  created the DB, and applied `0001_init.sql`; the warm path skipped the build.

### Negative

- Hard dependency on Python 3.12+ being the resolved `py -3`. If an older
  interpreter is first on PATH, the server won't start (it says so loudly).
- First-session latency during the cold build.

### Neutral / forward-looking

- Cross-platform support (Linux/macOS `python3` + `bin/`, per-OS `.mcp.json`)
  and revisiting PyPI/`uvx` are tracked as a roadmap step, not a v0.4.0 blocker.
- A bundled standalone runtime (Option C) remains available later if the
  Python-on-PATH prerequisite proves painful in practice.

## References

- [`docs/spike-0.md`](../spike-0.md) — plugin runtime behavior, silent-failure
  mode, `${PLUGIN_ROOT}` substitution, venv relocatability (§6)
- [`bootstrap.py`](../../bootstrap.py) — the launcher
- [`server/tests/test_bootstrap.py`](../../server/tests/test_bootstrap.py) —
  marker/atomic-swap/lock/exec unit tests
- [`docs/decisions/0001-stdio-vs-long-lived-backend.md`](0001-stdio-vs-long-lived-backend.md)
  — ADR format precedent and the stdio-per-session model this builds on
