# Phase 3 — Next-Session Steps (pick up here)

**Status:** Phase 3 (agent identity layer) is implemented, green (**103 tests
pass**, `ruff check` / `ruff format --check` clean), end-to-end verified through
the real Copilot CLI, and committed. This document is the to-do list for the
next session.

> Order matters: do the **8-agent review first** (it may change the code), then
> write the **ADR** for the design gap, then do the **clean install** last so it
> validates the final, reviewed state.

---

## How to get back to a working state

```powershell
cd C:\Repos\NP\NP.CoPilot.AgentMemory
$env:PYTHONPATH = "$(Get-Location)\server"
# Run tests
.\.venv\Scripts\python.exe -m pytest server\tests\ -q
# Lint + format check
.\.venv\Scripts\python.exe -m ruff check server
.\.venv\Scripts\python.exe -m ruff format --check server
```

Environment: Windows / PowerShell. `mcp==1.26.0`, `python-ulid==3.1.0`,
pytest 9.0.3, ruff 0.15.15. Venv is at the **repo root** `.venv` (NOT `server/`).

## What Phase 3 delivered (so reviewers know the surface)

- `server/np_agent_memory/identity.py` — `canonicalize_agent_cwd` (absolute
  validate → resolve symlinks → `normcase` → forward-slash → anchor-aware
  trailing-slash strip; rejects empty/relative/missing/non-dir), `new_ulid`,
  `now_iso`.
- `server/np_agent_memory/db.py` — `run_in_write_txn` / `run_in_read_txn`:
  whole-unit-of-work retry on `SQLITE_BUSY`/`LOCKED` only, `BEGIN IMMEDIATE` /
  `BEGIN DEFERRED`. Per-call connections (unchanged from Phase 2).
- `server/np_agent_memory/tools/agents.py` — `register_agent` / `describe_agent`
  / `add_alias` logic (take a `sqlite3.Connection`) + thin `@mcp.tool` wrappers
  registered via `register_agent_tools(mcp)`.
- `server/np_agent_memory/tools/__init__.py` — `register_all_tools(mcp)` wiring,
  called once in `__main__.py`.
- `docs/spike-roots.md` — the mandated `roots` probe result.
- `server/tests/test_agents.py` — 24 new tests.

**Key design facts (already settled, do not re-litigate):**
- Copilot CLI does **not** support MCP `roots` (probed live —
  `docs/spike-roots.md`). `agent_cwd` is **required** on every agent-scoped
  tool. No `roots` fallback exists by design.
- Identity invariant: **one canonical directory == one agent.** Symlink
  resolution intentionally merges paths; `agent_add_alias` merges residual
  Windows path variants (`subst`, UNC, extended-length prefixes).
- **Agents never see internal ULIDs** — tool responses return name/path only.

---

## 1. Full-scale Phase 3 review — 8 fresh-session agents

Run the same review ritual used for Phase 2 (`docs/phase-2-final-review-handover.md`):
8 fresh-session reviewers against the committed Phase 3 tree, collect findings,
fix ALL of them (including 🟢 LOW nits), then re-green tests/ruff.

Suggested reviewer set (pick 8, default + GPT variants for spread):
`architect`, `backend-developer`, `database-engineer`, `systems-engineer`,
`qa-engineer`, `security-engineer`, plus `code-reviewer` / `rubber-duck`
passes. Weight toward concurrency, canonicalization edge cases, and the
identity model.

Specific things to pressure-test:
- Canonicalization on Windows: `subst` drives, UNC vs mapped drive, `\\?\`
  extended-length prefixes, junctions/symlinked git work-trees, 8.3 short
  names. Confirm the "best-effort + aliases" stance holds.
- Concurrency: two processes registering the **same new** `agent_cwd`
  simultaneously must resolve to one agent (BEGIN IMMEDIATE + whole-txn retry
  re-selects on the losing process). Stress it with N concurrent processes.
- `describe_agent` snapshot consistency under concurrent writes (single read
  txn).
- `add_alias` conflict/no-op semantics and the unregistered-source error path.
- Non-destructive metadata updates (omitted `workstream`/`description` never
  erase stored values).
- Error surfacing: soft `{registered: false}` for describe vs raised
  `ValueError` for register/add_alias — is this consistent enough for the CLI?

Record the round in a living `docs/phase-3-review-actions.md` (mirror the
Phase 2 actions doc).

## 2. ADR for the design gap — cross-agent addressing / public handle

Write an ADR in `docs/decisions/` (next number after `0001`) capturing the gap
the rubber-duck flagged this session:

> Agent `name` is **not unique** in the schema. Future `inbox_send`-by-name
> (Phase 6) and any cross-agent addressing are therefore ambiguous. Agents
> never see internal ULIDs, so there is currently **no stable public
> identifier** other than the canonical path.

The ADR should evaluate options and pick one (with consequences):
- **A.** Add a public `agent_key` / slug column (human-friendly, immutable,
  unique) returned by `agent_register` and accepted by addressing tools.
- **B.** Enforce `UNIQUE(name)` (or `UNIQUE(workstream, name)`) and address by
  name.
- **C.** Address strictly by canonical path (no name addressing) — simplest,
  but paths are not always known to the sender.

Note migration impact: any new column/constraint is a **new** migration
(`0002_*.sql`); do **not** edit `0001_init.sql` (checksum guard). Coordinate
with whatever Phase 5/6 inbox work needs.

## 3. Clean install (do this LAST)

After review fixes are committed and the ADR is in, validate the production
path from scratch (the install dir was hand-synced during the Phase 3 `roots`
probe / e2e and is NOT a clean marketplace install):

```powershell
cd C:\Repos\NP\NP.CoPilot.AgentMemory
# Optional: remove the hand-synced install + its marketplace first
copilot plugin uninstall np-agent-memory@np-agent-memory-marketplace
copilot plugin marketplace remove np-agent-memory-marketplace

# Rebuild the bundled venv with pinned deps (now includes python-ulid)
.\install.ps1

# Reinstall from the local marketplace
copilot plugin marketplace add C:\Repos\NP\NP.CoPilot.AgentMemory
copilot plugin install np-agent-memory@np-agent-memory-marketplace
```

Then, from a fresh CLI session, smoke-test:
`agent_register` → `agent_describe` → `agent_add_alias` round-trip, and confirm
`install.ps1` self-verify passed. If you registered any throwaway agents during
testing, delete them from the production DB
(`$HOME\.copilot\np-agent-memory\agent-memory.db`) before wrapping.

---

## After the three steps

1. Re-run tests + ruff (commands at top) — all green/clean.
2. Present the change summary to Nick and use `ask_user` for commit-message
   approval (Conventional Commit, `feat(phase-3): ...` / `fix(phase-3): ...`,
   with the `Co-authored-by: Copilot ...` trailer). **Do not commit before
   Nick approves.**
3. Update `docs/TASKS.md` status if Phase 3 scope shifts, and move on to
   Phase 4 (memory + todos tools) per `docs/TASKS.md`.
