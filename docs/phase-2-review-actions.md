# Phase 2 Review Actions

Post-review action items for Phase 2 (Data folder + migration runner).
These must be addressed **before committing Phase 2** and moving to Phase 3.

References:
- Plan: [`docs/PLAN.md`](PLAN.md)
- Tasks: [`docs/TASKS.md`](TASKS.md) — Phase 2

## Source Reviews (2026-05-28)

Four parallel reviewers assessed the implementation:

1. **Backend Developer** — Python code quality, API design, error handling
2. **QA Engineer** — Test validity, coverage gaps, reliability
3. **Systems Engineer** — Multi-process safety, WAL correctness, failure modes
4. **Splitter Specialist** — Deep analysis of `_split_statements()` parser

---

## 🔴 Must Fix Before Commit

### F1. `_split_statements()` — Line comments desync quote state

**Problem:** `-- don't break this` contains an apostrophe. The current
character-by-character parser doesn't know it's inside a comment, so it
toggles `in_quote` and subsequent semicolons may be incorrectly handled.
This is a latent bug — any future migration with a natural English comment
containing an apostrophe will silently merge or drop statements.

**Fix:** Replace with index-based iteration that detects `--` outside of
quotes and consumes the rest of the line without tracking quote state.
The splitter specialist provided a corrected implementation — apply it.

**Location:** `server/np_agent_memory/migrations/__init__.py` → `_split_statements()`

---

### F2. `_split_statements()` — Double-quoted identifiers not tracked

**Problem:** SQLite allows `"quoted;identifiers"` with semicolons. The
parser only tracks single quotes. A migration using quoted identifiers
with semicolons will be mis-split.

**Fix:** Add double-quote state tracking alongside single-quote in the
corrected implementation.

**Location:** Same as F1.

---

## 🟡 Should Fix Before Commit

### F3. Use `IF NOT EXISTS` in DDL for crash resilience

**Problem:** If a process crashes mid-migration and WAL recovery fails
(rare but possible on disk corruption), the DB may have partial DDL applied
but no `migrations` record. Re-running the migration hits "table already
exists". Using `IF NOT EXISTS` makes migrations idempotent in this scenario.

**Fix:** Change all `CREATE TABLE` → `CREATE TABLE IF NOT EXISTS` and
`CREATE INDEX` → `CREATE INDEX IF NOT EXISTS` in `0001_init.sql`.

**Location:** `server/np_agent_memory/migrations/0001_init.sql`

---

### F4. Migration busy_timeout × retry creates 28s worst-case startup delay

**Problem:** The migration connection inherits `busy_timeout = 5000` from
`_configure_connection()`. Combined with 5 retries (0.1s + 0.2s + 0.4s +
0.8s + 1.6s sleep), worst-case startup is ~28 seconds before failure.

**Fix:** Use a shorter `busy_timeout` (1000ms) for the migration
connection specifically, or increase `_MAX_RETRIES` to 8 and reduce
`_BASE_DELAY_S`. The goal: fail faster on true deadlocks, succeed on
transient locks.

**Location:** `server/np_agent_memory/migrations/__init__.py` → `_open_migration_connection()`

---

### F5. No tests for `memory_alive` tool / `__main__.py`

**Problem:** The MCP entry point and `memory_alive()` tool have zero test
coverage. If a key is missing or the type changes, clients break silently.

**Fix:** Add `TestMemoryAliveTool` class covering:
- Returns all expected keys with correct types
- `db_path` is `None` before `main()` runs
- `uptime_seconds` increases over time

**Location:** New file `server/tests/test_main.py`

---

## 🟢 Acknowledged — Non-Blocking (Phase 3 Follow-ups)

| Item | Disposition |
|------|-------------|
| `/* */` block comments not handled | Document limitation; unused in this codebase |
| `synchronous=NORMAL` durability trade-off | Acceptable; add doc comment |
| Cloud sync (OneDrive) file-lock risk | Document exclusion in README |
| No `__all__` exports in modules | Add when Phase 3 ships public API |
| Module-level `_STARTED_AT` timing | Cosmetic; defer |
| Tests import private members | Pragmatic for this project size |

---

## Additional Tests to Add (from QA review)

Alongside the fixes above, add these tests:

```
TestSplitStatements:
  test_apostrophe_in_line_comment
  test_semicolon_in_line_comment
  test_comment_after_code_on_same_line
  test_multiple_escaped_quotes_with_semicolons
  test_semicolon_inside_double_quoted_identifier

TestMemoryAliveTool:
  test_returns_expected_keys
  test_db_path_none_before_init
  test_uptime_increases

TestConcurrentMigrations:
  (harden) assert not t.is_alive() after join
  (harden) use threading.Event for retry test synchronization
```

---

## Execution Order

1. Apply the corrected `_split_statements()` (F1 + F2)
2. Update `0001_init.sql` with `IF NOT EXISTS` (F3)
3. Tune migration `busy_timeout` (F4)
4. Add new tests (F5 + additional tests list)
5. Run full test suite — all must pass
6. Final self-review
7. Commit Phase 2: `feat(phase-2): data folder, migration runner, and initial schema`
8. Update `docs/TASKS.md` — mark Phase 2 done ✅
