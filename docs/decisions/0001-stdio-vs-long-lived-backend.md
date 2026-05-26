# ADR 0001 — stdio MCP server per session vs. long-lived backend (Docker / service)

- **Status:** Accepted
- **Date:** 2026-05-26
- **Decider:** Nick Polyderopoulos
- **Context window:** Post-Phase-0 spike, pre-Phase-1 kickoff
- **Related:** [`docs/PLAN.md`](../PLAN.md), [`docs/spike-0.md`](../spike-0.md)

## Context

Phase 0 ([`docs/spike-0.md`](../spike-0.md)) validated that the Copilot CLI
plugin mechanism can launch a Python stdio MCP server per session from a
bundled venv. The spike also confirmed the multi-process reality: one MCP
server process is spawned per Copilot CLI invocation (roughly one per
terminal window), so multiple concurrent server processes will run against
the same SQLite file on a busy day.

This raises a natural design question — restated by Nick on 2026-05-26:

> "Do we need to let every session create an MCP server? We could possibly
> just run a Docker container and have the MCP connect to that container.
> What would the implications be on installation?"

The real choice is **stdio (process per session)** vs. **a long-lived
backend over HTTP** (hosted in Docker, a Windows Service, or a startup
task). MCP supports both transports, so this is a genuine architectural
decision, not a constraint.

This ADR records why we chose stdio for v1, what we'd give up by going
backend, and the conditions under which we'd revisit the decision.

## Options considered

### Option A — stdio MCP server per session (chosen)

The model validated in spike-0:

- `.mcp.json` declares a `"type": "local"` server with `command =
  ${PLUGIN_ROOT}/.venv/Scripts/python.exe` and `args = ["-m",
  "np_agent_memory"]`.
- Copilot CLI spawns one server per session; the server speaks MCP over
  stdio with that CLI.
- Shared state lives in SQLite (`$HOME\.copilot\np-agent-memory\memory.db`)
  in WAL mode. Concurrency between server processes is handled by
  `busy_timeout=5000`, `BEGIN IMMEDIATE` for migrations, and per-call
  connections (no shared in-memory state across processes).

### Option B — Long-lived backend (Docker container) + thin per-session shim

A single backend process runs continuously. Copilot CLI sessions connect
over HTTP/SSE. The plugin would still need to ship *something* the CLI can
launch via `.mcp.json` — most realistically a tiny stdio shim that proxies
MCP messages to the backend's HTTP endpoint, since plugin transport in
Copilot CLI is stdio-first (and HTTP-from-plugin is untested as of
spike-0).

### Option C — Long-lived backend as a Windows Service / scheduled startup task

Same as Option B but hosted natively on Windows. Removes the Docker
dependency but keeps every other cost of running a long-lived backend
(lifecycle, observability, install complexity, network surface).

## Decision

**Adopt Option A — stdio MCP server per session — for v1.**

We will revisit if and when one of the explicit triggers below is met
(see "When we'd revisit").

## Rationale

The spike validated that Option A works end-to-end with no significant
operational complexity. The benefits of Option B/C are real but small at
our scale; their costs are real and large.

### What we'd gain from a long-lived backend

1. **Single SQLite writer.** No WAL contention, no `BEGIN IMMEDIATE`
   dance, no `busy_timeout` retries. ~30–50 lines of concurrency code
   removed.
2. **Shared in-memory state.** Caches, prepared statements, a real
   connection pool.
3. **Lower per-session cost.** No Python interpreter + venv cold-start
   each window (~200–500ms saved per session).
4. **Centralized observability.** One log file, one process.
5. **A natural home for background jobs.** Scheduled vacuum, backup
   rotation, inbox TTL, daemon health checks.

### What it would cost

1. **Docker Desktop becomes a hard prerequisite (Option B).** WSL2,
   several GB of disk, Docker's licensing terms for organizational use,
   and a Docker process the user must keep running. Phase 0 was painless
   precisely because the install was "bundled venv + one PowerShell
   script." Docker turns the install story into "Docker Desktop
   installed and running, image pulled, container started, container
   survives reboots, container health monitored." Option C avoids the
   Docker dependency but keeps the rest.

2. **Copilot CLI plugin transport is stdio-first.** Spike-0 used
   `"type": "local"` with `command`+`args`. HTTP-from-plugin is not
   confirmed. Even if it works, the plugin still needs *something* the
   CLI can launch — realistically a small stdio shim that proxies to the
   backend. You don't eliminate the per-session process; you make it
   dumber. The latency and cold-start wins are smaller than they look.

3. **Lifecycle management becomes the plugin's problem.**
   - Who starts the backend on boot?
   - What happens when Docker Desktop restarts?
   - If the backend is down, every Copilot session silently breaks
     (spike-0 already showed stdio failures are silent to the agent;
     HTTP backends have more silent-failure modes — port conflicts,
     dead container, crashed daemon).

4. **Windows ↔ Linux filesystem boundary (Option B).** Container is
   Linux. DB lives at `$HOME\.copilot\np-agent-memory\`. Three bad
   options:
   - Bind-mount the Windows path into the container — SQLite perf is OK
     across WSL2 but file-locking semantics differ subtly, and Windows
     backup tools can race with the in-container writer.
   - Store the DB inside a Docker volume — Windows-native tools can no
     longer easily read or back it up.
   - Run a sync process — extra moving part, more failure modes.

5. **New local network surface.** HTTP-on-localhost means any local
   process can hit `127.0.0.1:PORT` and read/write agent memory. Stdio
   restricts access to the spawning Copilot CLI process. Solvable (auth
   token, Unix socket equivalent) but a new surface to defend.

6. **Per-call latency.** Stdio is in-process microseconds. Localhost
   HTTP via Docker Desktop on Windows is typically 10–50ms per call due
   to WSL2 networking. For tools that may be called multiple times per
   turn (`note_add`, inbox checks, query tools), this compounds.

7. **It doesn't solve a problem we have.** Spike-0 explicitly validated
   the multi-process SQLite-WAL design. SQLite with `busy_timeout=5000`
   and `BEGIN IMMEDIATE` for migrations is a well-understood pattern,
   small in code, with predictable failure modes. Trading 30 lines of
   concurrency code for "Docker dependency + lifecycle daemon + install
   complexity + new failure modes" is a bad trade at v1.

### Nuance: background work doesn't require a backend

If we later need scheduled background tasks (backups, vacuum, inbox
TTL), a **Windows Scheduled Task** running the same Python module
against the same SQLite file gets us most of the benefit with zero new
transport. SQLite is already the shared-state primitive. We don't need
a long-lived backend to host background jobs.

## Consequences

### Positive

- Install story stays trivial: bundled venv + one PowerShell script. No
  Docker prerequisite, no daemon to manage.
- Failure modes are local to a single CLI session — one broken plugin
  install doesn't take down agent memory for every window.
- Per-call latency is microseconds, not milliseconds.
- No new network surface to authenticate or defend.
- DB file is a plain Windows file, readable by native Windows tools
  (backup, diff, sqlite3.exe) with no boundary crossing.

### Negative

- We accept ~30–50 lines of SQLite concurrency code (WAL mode,
  `busy_timeout`, `BEGIN IMMEDIATE` for migrations, per-call
  connections, no shared in-memory state). This is captured in
  `docs/PLAN.md` and is the cost of multi-process.
- Per-session cold-start cost (~200–500ms) is paid every time a new
  Copilot CLI window launches. Acceptable for an interactive tool.
- We cannot use process-local caches across sessions. All cross-session
  state must round-trip through SQLite. (In practice this is what we
  want anyway — the DB is the source of truth.)

### Neutral / forward-looking

- **This is not a one-way door.** The tools layer (`server/tools/`) is
  transport-agnostic. If a future trigger materialises (see below), we
  can introduce an HTTP variant in v2 without rewriting business logic.
- The `agent_cwd` canonicalization design from spike-0 §8 stays correct
  under both transports — agent paths are stored as opaque keys, not
  used as filesystem paths.

## When we'd revisit

Explicit triggers that would justify reopening this ADR and moving to
Option B or C:

1. **Cross-machine memory.** Nick wants agents on a second machine
   (laptop, work VM) to share the same memory and inbox. A long-lived
   backend reachable over the network is the natural fit; per-machine
   stdio servers against a shared SQLite file over a network mount is
   not.
2. **Background jobs grow beyond what a Windows Scheduled Task can
   sensibly own** — e.g. a real daemon doing webhook fan-out, push
   notifications, or maintaining live indices.
3. **Measured contention on SQLite WAL** becomes a real problem
   (sustained busy-timeout retries, observed latency spikes). Phase 0
   suggests this is unlikely at the volumes we expect, but if it
   happens, it's a hard signal.
4. **A new agent tool that genuinely needs shared in-memory state**
   across sessions (e.g. an in-process queue or pubsub fabric). Most
   things we'd want here can be expressed as DB rows; if not, the
   backend model becomes attractive.

Until then, stdio per session is the right shape.

## References

- [`docs/spike-0.md`](../spike-0.md) — Phase 0 spike findings (the
  empirical basis for this ADR)
- [`docs/PLAN.md`](../PLAN.md) — production design (assumes stdio +
  SQLite-WAL multi-process)
- [`docs/TASKS.md`](../TASKS.md) — phased delivery plan
- MCP transports: stdio vs. Streamable HTTP — see MCP spec
