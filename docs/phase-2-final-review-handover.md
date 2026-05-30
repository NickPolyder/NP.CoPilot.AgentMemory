# Phase 2 — Final Review Handover (pick up here tomorrow)

**Status:** Phase 2 (migration runner, "Path A" rewrite) is implemented, green
(62 tests pass, `ruff check`/`ruff format --check` clean), and **uncommitted**.
A final round of **8 fresh-session reviewers** ran on the current working tree.
This document is the authoritative to-do list to clear before committing Phase 2.

> Nick wants ALL findings below addressed (including the 🟢 LOW nits), then a
> commit-message approval via `ask_user`, then commit. Do **not** commit before
> Nick approves the message.

> ⚠️ **This working tree was committed and pushed as a WIP checkpoint purely for
> data safety** — it is **NOT** reviewed-and-approved work. The findings below
> are still OPEN and must be fixed. When you address them, build on top with new
> commits (do not assume the WIP commit is the final Phase 2 commit; we may
> squash/reword it when the work is clean). The 8-reviewer round above ran
> against exactly this committed state.

---

## How to get back to a working state

```powershell
cd C:\Repos\NP\NP.CoPilot.AgentMemory
$env:PYTHONPATH = "$(Get-Location)\server"
# Run tests
.\.venv\Scripts\python.exe -m pytest server\tests\ -q
# Lint + format check
.\.venv\Scripts\ruff.exe check server
.\.venv\Scripts\ruff.exe format --check server
```

Environment: Windows / PowerShell. Python 3.14.5, SQLite 3.50.4, `mcp==1.26.0`,
pytest 9.0.3, ruff 0.15.15. Venv is at the **repo root** `.venv` (NOT `server/`).

Uncommitted working-tree files (vs HEAD `21ba2d2`):
`README.md`, `docs/TASKS.md`, `docs/phase-2-review-actions.md`,
`server/np_agent_memory/__main__.py`, `server/np_agent_memory/db.py`,
`server/np_agent_memory/migrations/0001_init.sql`,
`server/np_agent_memory/migrations/__init__.py`,
`server/tests/test_db.py`, `server/tests/test_migrations.py`,
plus NEW: `pyproject.toml`, `requirements-dev.txt`, `server/tests/test_main.py`.

---

## Context: what "Path A" is (so you understand the code you're hardening)

The migration runner applies each `NNNN_*.sql` file atomically under a
Python-owned `BEGIN IMMEDIATE`, using a **fresh `autocommit=True` connection per
attempt**. The load-bearing discovery: under Python 3.12+
`sqlite3.connect(..., autocommit=True)`, a Python-issued `BEGIN IMMEDIATE`
followed by `conn.executescript(body)` does **NOT** force-commit — the
transaction stays open (`in_transaction == True`), so we can do
check-before-DDL + bookkeeping insert + `COMMIT` all in one transaction. (Legacy
`isolation_level=None` force-commits on `executescript`; that's why the old
hand-rolled statement splitter existed. The splitter is now deleted.)

Per-migration flow in `_apply_migration` (`migrations/__init__.py:266-348`):
fresh `autocommit=True` conn → `_configure_connection` (busy_timeout, WAL,
foreign_keys, all pre-BEGIN) → `BEGIN IMMEDIATE` → **check `migrations` row
under the write lock BEFORE any DDL** (race-loser never runs the body) →
`executescript(sql)` → guard → insert bookkeeping row → `COMMIT`. Busy retry
with exp backoff+jitter, bounded at `_MAX_RETRIES=5`.

This design (check-before-DDL race safety, crash-mid-migration atomicity) was
**empirically confirmed sound** by all 8 reviewers under 16–32 concurrent
processes. The findings below are hardening + one real atomicity gap, NOT a
redesign.

---

## Final review outcome (8 fresh-session reviewers)

Verdict split was **4 SHIP-with-nits (all default model) vs 4 DO-NOT-SHIP (all
GPT-5.5)** — but this is severity weighting, not factual disagreement. **All 8
flagged the same #1 issue.** Reviewer agents:
`final-{backend,systems,qa,migration}-{default,gpt}`.

---

## TO-DO before commit (in priority order)

### 🔴 1. Reject transaction-control statements BEFORE `executescript` (CRITICAL — all 8)

**Problem.** The current guard checks `conn.in_transaction` *after*
`executescript` (`migrations/__init__.py:305`). That is a tripwire, not a safety
net:
- A body `create t; commit; create u;` commits `t`+`u` permanently before the
  guard fires; the migration is then recorded as **unapplied** while the schema
  is mutated → next startup fails or loops. Verified by 4 reviewers.
- A body `create t; commit; begin; create u;` even **passes** the guard
  (`in_transaction == True`) with atomicity silently broken.

**Fix (validated empirically this session — use this approach).** Install a
SQLite authorizer that denies transaction control at *prepare* time, scoped to
ONLY the `executescript` call. When denied, the statement never executes, the
outer `BEGIN IMMEDIATE` stays open, and the existing `except → ROLLBACK` undoes
ALL partial DDL (verified: zero leaked tables). Valid DDL/triggers/views/indexes
are unaffected.

```python
def _deny_transaction_control(action, _a1, _a2, _db, _trigger):
    # SQLITE_TRANSACTION covers BEGIN/COMMIT/ROLLBACK/END/RELEASE;
    # SQLITE_SAVEPOINT covers SAVEPOINT/RELEASE.
    if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK
```

Scope it tightly — the authorizer must be cleared BEFORE the bookkeeping
`COMMIT`, or that COMMIT is denied too (this bit me in the probe):

```python
conn.set_authorizer(_deny_transaction_control)
try:
    conn.executescript(sql)
finally:
    conn.set_authorizer(None)   # MUST clear before the bookkeeping COMMIT
# keep the existing `if not conn.in_transaction: raise` as belt-and-suspenders
# insert bookkeeping row, then COMMIT
```

A denied statement raises `sqlite3.DatabaseError("not authorized")`. Convert it
into the existing clear `RuntimeError("Migration ... must not contain
BEGIN/COMMIT/ROLLBACK ...")` so the message stays actionable. Keep the
post-`executescript` `in_transaction` check as a second line of defense.

**Tests to add/fix** (`server/tests/test_migrations.py`):
- Parametrize `test_transaction_control_in_body_rejected` over
  `COMMIT`, `ROLLBACK`, `END`, `SAVEPOINT sp; RELEASE sp;` (currently only
  `COMMIT`).
- For each: assert (a) `RuntimeError` raised, (b) **no** bookkeeping row, and
  (c) **no leaked user tables** (e.g. neither `t` nor `u` exists). The leaked-
  table assertion is the one the current test is missing and is what lets the
  bug pass today.

### 🟠 2. Enforce the Python 3.12+ runtime contract (HIGH — 4 reviewers)

`sqlite3.connect(..., autocommit=True)` and `datetime.UTC` require Python 3.12+.
Today README says "3.10+" and `install.ps1` doesn't check → silent install then
cryptic `TypeError: 'autocommit' is an invalid keyword` at first migration.

- `README.md` (~line 50): change "Python 3.10+" → "Python 3.12+".
- `install.ps1` (~line 42-49): fail-fast — check the venv interpreter is
  `>= 3.12` (e.g. run `python -c "import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)"`
  and emit an actionable ❌ error on failure). Keep it idempotent,
  `$ErrorActionPreference = 'Stop'`, emoji status output (repo PS style).
- `pyproject.toml`: add a `[project]` table with `requires-python = ">=3.12"`
  (currently the file has only `[tool.ruff]`).

### 🟡 3. Medium hardening

- **Multi-migration sequencing tests** (qa-default, qa-gpt). Today only
  `0001_init.sql` exists, so the `for version, path in migrations:` loop in
  `run_migrations` never runs >1×, and `test_versions_are_ascending` /
  `test_finds_sql_files_in_order` are tautological (`[1] == sorted([1])`). Add
  tests that `monkeypatch`/`patch.object` `_MIGRATIONS_DIR` to a temp dir with
  `0001`/`0002`/`0003` (0002 referencing a table from 0001 to prove ordering):
  - apply all three in one call → 3 rows, versions `[1,2,3]`, checksums match;
  - incremental: apply `{0001}`, then add `0002`, re-run → 0001 not re-applied,
    only 0002 newly recorded;
  - scrambled filename creation order still sorts ascending by version.
- **WAL-verification `RuntimeError` bypasses the busy-retry loop**
  (systems-default, migration-default, systems-gpt). `db.py:115-120`
  `_configure_connection` raises `RuntimeError` if `PRAGMA journal_mode=WAL`
  returns a non-`wal` row, but `_retry_on_busy` only catches
  `sqlite3.OperationalError` (`migrations/__init__.py:183`). This contradicts
  the `db.py:88-92` comment that says the conversion "can silently fail ...
  return 'delete'" under a checkpoint lock. **Pick one:** either make that path
  raise a busy-classified `OperationalError` the loop retries, OR delete the
  contradicting comment and accept it's fatal. (Low real-world risk — in
  practice SQLite raises BUSY here, which *is* retried — but resolve the
  inconsistency.)
- **Missing FK-supporting index on `inbox.from_agent_id`**
  (`0001_init.sql`, migration-gpt). `inbox.from_agent_id` references
  `agents(id) on delete restrict` but only `to_agent_id` is indexed; a parent
  delete/restrict check scans `inbox`. Add:
  `create index if not exists idx_inbox_from_agent on inbox(from_agent_id);`
  **⚠️ Note:** editing `0001_init.sql` changes its checksum, which the runner
  treats as a fatal "do not edit shipped migrations" error for any DB that
  already applied the old 0001. This is fine pre-release (nothing shipped) but
  **you must wipe any dev/CI DB** (`$HOME\.copilot\np-agent-memory\` or
  `AGENT_MEMORY_DIR`) after this change. Add the index in the SAME commit as the
  other 0001 edits so there's a single checksum bump.
- **pytest pin mismatch** (`requirements-dev.txt:4`, backend pair). Pinned
  `pytest==9.0.1` but the validated/installed version is `9.0.3`. Bump to
  `9.0.3`.
- **`_apply_migration` retry branch not directly tested** (qa-gpt).
  `test_retry_on_database_locked` takes its exclusive lock before
  `run_migrations`, so the first retry happens during bootstrap, not in
  `_apply_migration`. Add a test where the `migrations` table already exists and
  a peer holds `BEGIN IMMEDIATE`, released inside a patched `time.sleep`, and
  assert `_apply_migration` retries then succeeds.
- **Divergent-checksum race test** (`test_divergent_checksum_race_raises`,
  qa pair) asserts only the raise. Add `assert "t" not in sqlite_master` to
  prove the loser never ran the DDL body (symmetry with the benign-race test).

### 🟢 4. Low nits (Nick asked to include these)

- **Version regex caps at 4 digits / sorts by string** (`migrations/__init__.py:45`,
  migration-default). `^(\d{4})_.+\.sql$` silently skips `10000_x.sql` and
  `1_x.sql` with no warning, and discovery relies on zero-padded string sort.
  Widen to `\d{4,}` and sort by the parsed integer version as a safety net.
- **`per-file-ignores` glob too narrow** (`pyproject.toml:26`, backend-default).
  `"server/tests/*"` won't match nested test subdirs. Change to
  `"server/tests/**"`.
- **Redundant `extend-exclude = [".venv"]`** (`pyproject.toml:10`) — `.venv` is
  in ruff's default excludes. Harmless; remove for tidiness.
- **Document the dev-DB-wipe** caused by the `0001_init.sql` checksum change
  (see 🟡 FK index note) in README dev instructions / release notes.
- **Document `executescript`-in-transaction limitations for future authors**
  (migration-default): migrations run inside `BEGIN IMMEDIATE`, so future files
  must not use `VACUUM`, `PRAGMA journal_mode`, or other statements that must
  run outside a transaction. Add to the convention docstring at
  `migrations/__init__.py:23-28` alongside the existing "no transaction control"
  rule.

---

## Schema observations (no action required, just confirm intent)

- `idx_inbox_to_unread_prio ... priority` — `priority` is stored as text
  (`'low'/'normal'/'high'/'urgent'`), so `ORDER BY priority` is alphabetical,
  not semantic. Ensure query code maps priority→rank rather than ordering on raw
  text. (Relevant in later phases, not Phase 2.)
- `blockers unique (agent_id, external_key)` with nullable `external_key`:
  SQLite treats NULLs as distinct, so multiple NULL-`external_key` blockers per
  agent are allowed. Almost certainly intended (external_key optional).

---

## After fixes are in

1. Re-run tests + ruff (commands at top). All must be green/clean.
2. Append a short "Path A final-review round" entry to
   `docs/phase-2-review-actions.md` (the living review record).
3. Update the stored memory about `executescript` (currently:
   "Python sqlite3.executescript() always issues an implicit COMMIT first ...
   Never use it inside BEGIN IMMEDIATE"). It is **partially outdated** — true
   for legacy `isolation_level=None`, but NOT for `autocommit=True` (3.12+),
   which is exactly what Path A relies on. Refine it.
4. Present the change summary to Nick and use `ask_user` to get commit-message
   approval. Conventional Commit, e.g. `feat(phase-2): ...` with trailer
   `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.
   **Do not commit before Nick approves the message.**

---

## Why this round found a real issue (Nick's standing question)

Earlier rounds mostly surfaced hardening/coverage. This round found exactly ONE
genuinely new substantive bug — the late transaction-control guard — which is a
real atomicity gap reproducible with a 3-line migration. It was reproduced and a
fix validated this session (the authorizer approach). Everything else is
release-hygiene and test-coverage polish. The 4-4 verdict split tracked the
model (default vs GPT-5.5), not the facts: all 8 agreed the bug exists; they
disagreed only on whether it blocks a pre-release internal commit.
