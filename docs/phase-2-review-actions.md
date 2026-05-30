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

### F1. `_split_statements()` — Line comments desync quote state ✅ FIXED

**Problem:** `-- don't break this` contains an apostrophe. The current
character-by-character parser doesn't know it's inside a comment, so it
toggles `in_quote` and subsequent semicolons may be incorrectly handled.
This is a latent bug — any future migration with a natural English comment
containing an apostrophe will silently merge or drop statements.

**Fix applied:** Rewrote with index-based iteration that detects `--` outside
of quotes and consumes the rest of the line without tracking quote state.

**Location:** `server/np_agent_memory/migrations/__init__.py` → `_split_statements()`

---

### F2. `_split_statements()` — Double-quoted identifiers not tracked ✅ FIXED

**Problem:** SQLite allows `"quoted;identifiers"` with semicolons. The
parser only tracks single quotes. A migration using quoted identifiers
with semicolons will be mis-split.

**Fix applied:** Added double-quote state tracking alongside single-quote in
the corrected implementation.

**Location:** Same as F1.

---

## 🟡 Should Fix Before Commit

### F3. Use `IF NOT EXISTS` in DDL for crash resilience ✅ FIXED

**Problem:** If a process crashes mid-migration and WAL recovery fails
(rare but possible on disk corruption), the DB may have partial DDL applied
but no `migrations` record. Re-running the migration hits "table already
exists". Using `IF NOT EXISTS` makes migrations idempotent in this scenario.

**Fix applied:** Changed all `CREATE TABLE` → `CREATE TABLE IF NOT EXISTS` and
`CREATE INDEX` → `CREATE INDEX IF NOT EXISTS` in `0001_init.sql`.

**Location:** `server/np_agent_memory/migrations/0001_init.sql`

---

### F4. Migration busy_timeout × retry creates 28s worst-case startup delay ✅ FIXED

**Problem:** The migration connection inherits `busy_timeout = 5000` from
`_configure_connection()`. Combined with 5 retries (0.1s + 0.2s + 0.4s +
0.8s + 1.6s sleep), worst-case startup is ~28 seconds before failure.

**Fix applied:** Override migration connection `busy_timeout` to 1000ms after
`_configure_connection()`. Worst-case is now ~8s, well within acceptable startup.

**Location:** `server/np_agent_memory/migrations/__init__.py` → `_open_migration_connection()`

---

### F5. No tests for `memory_alive` tool / `__main__.py` ✅ FIXED

**Problem:** The MCP entry point and `memory_alive()` tool have zero test
coverage. If a key is missing or the type changes, clients break silently.

**Fix applied:** Added `TestMemoryAliveTool` (6 tests) and `TestMainFunction`
(1 test) in `server/tests/test_main.py`.

**Location:** `server/tests/test_main.py`

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

---

## Round 2 Review (4 reviewers) — addressed

After F1–F5, a second 4-reviewer round (backend, qa, systems, splitter) found
and we fixed: migration-table creation race (retry on bootstrap/read), fresh
connection per migration attempt, backoff jitter, `/* */` block-comment
support in `_split_statements`, WAL pragma return-value verification,
structured startup error handling in `__main__.py`, and `ON DELETE RESTRICT`
on child-table FKs in `0001_init.sql`.

---

## Round 3 Review (8 reviewers: original 4 × {default, GPT-5.5}) — addressed

Strong cross-model consensus surfaced two regressions introduced by the
round-2 refactor, plus several hardening items. All fixed:

### 🔴 CRITICAL A — Connection open outside retry + WAL/busy_timeout ordering ✅ FIXED

**Problem:** `_open_migration_connection()` ran `PRAGMA journal_mode = WAL`
during connection setup, which can raise `SQLITE_BUSY` during a concurrent
cold-start delete→WAL conversion. The open sat **outside** the retry `try`
block in both `_retry_on_busy()` and `_apply_migration()`, so the lock was
not retried *and* the connection leaked. Compounding it, `_PRAGMAS` set
`journal_mode = WAL` **before** `busy_timeout`, so the conversion ran with
zero lock tolerance.

**Fix:** Reordered `_PRAGMAS` so `busy_timeout` precedes `journal_mode = WAL`
(`db.py`). Moved `conn = _open_migration_connection(...)` **inside** the retry
`try` with a `conn = None` guard in `finally` (both call sites). Added
`_is_busy_error()` using `sqlite_errorcode` (SQLITE_BUSY/LOCKED) instead of
brittle substring matching. `_open_migration_connection()` now closes the
connection if configuration fails.

### 🔴 CRITICAL B — `_strip_leading_comments` dropped SQL after a leading comment ✅ FIXED

**Problem:** The line-based stripper discarded the entire line when a leading
`/* */` block comment shared a line with real SQL
(`/* header */ CREATE TABLE foo (...)`), silently dropping the statement while
recording the migration as applied.

**Fix:** Rewrote `_strip_leading_comments()` as a character-position scanner
that skips leading whitespace, `--` line comments, and `/* */` block comments
(returning `""` for an unterminated block comment) and preserves any SQL that
follows. Note: `_split_statements()` itself was verified correct and left
unchanged.

### 🟡 Hardening items ✅ FIXED

- Race-path checksum check: `_apply_migration` now selects `checksum` (not
  just `version`) in its in-transaction double-check and raises on mismatch.
- `connect()` in `db.py` closes the connection if `_configure_connection`
  raises (no descriptor leak on WAL verification failure).
- WAL verification treats a missing result row as a failure.
- `_discover_migrations()` rejects duplicate version numbers.
- `__main__.py` catches `sqlite3.OperationalError` / `sqlite3.Error` for
  permission / disk / corruption diagnostics (these are not `OSError`).

### Test-quality fixes ✅ DONE

- `test_retry_on_database_locked`: deterministic (lock released on first
  backoff via patched `time.sleep`), asserts a retry occurred, dead code
  removed, `check_same_thread=False` blocker.
- `test_parallel_migration_runs_no_duplicate_records`: `threading.Barrier`
  for real contention.
- Added: atomic-rollback test, duplicate-version rejection test, same-line
  block-comment regression tests, `_strip_leading_comments` direct tests,
  `main()` RuntimeError / sqlite OperationalError / generic-exit tests,
  WAL-verification-failure tests, `connect()` close-on-failure test.
- `main()` tests restore `_DB_PATH` via monkeypatch to avoid cross-test leak.

**Result:** 74 tests pass (up from 58); concurrency tests stable across
repeated runs.

### Round-3 re-review (8 idle reviewers re-engaged) — follow-ups ✅ FIXED

The four critical-finding reviewers (systems + splitter, both models)
confirmed CRITICAL A and B are fully fixed and leak-free. Two additional
valid items surfaced and were fixed:

- **`_is_busy_error` missed extended result codes** (systems-gpt, Important):
  `code in (5, 6)` would not match extended codes such as
  `SQLITE_BUSY_SNAPSHOT` (517). Now masks the primary class:
  `(code & 0xFF) in (5, 6)`. Added direct tests.
- **Misleading `db.py` pragma-order comment** (systems-default, Minor):
  empirically, `busy_timeout` does NOT help the delete→WAL conversion (that
  mode change raises `SQLITE_BUSY` immediately and bypasses the busy handler);
  the retry/backoff loop is what tolerates cold-start contention. Comment
  corrected so the retry loop is not mistaken as redundant. Ordering kept
  (harmless; busy_timeout still governs `BEGIN IMMEDIATE`/statement waits).

**Result:** 78 tests pass.

---

## Path A re-architecture (executescript + authorizer)

The split-statement parser (`_split_statements`) was removed entirely in favour
of **Path A**: run each migration body via `sqlite3.executescript` inside a
single runner-owned `BEGIN IMMEDIATE` transaction. The connection is opened with
`autocommit=True` (PEP 249, Python 3.12+) so the Python-issued `BEGIN IMMEDIATE`
survives `executescript` (legacy `isolation_level=None` would force-commit it).

### Path A final review (8 fresh-session reviewers: 4 roles × 2 models)

10 findings raised and addressed (see `docs/phase-2-final-review-handover.md`).
Highlights:

- 🔴 **Transaction-control guard** (all 8): replaced the post-hoc
  `in_transaction` tripwire with a SQLite **authorizer** that returns
  `SQLITE_DENY` for transaction-control statements at *prepare* time, scoped
  around `executescript` and cleared before the bookkeeping COMMIT. Empirically
  verified zero partial-DDL leak on the reviewer repro.
- 🟠 **Python 3.12+ enforced** in README, `install.ps1` (fail-fast venv check),
  `pyproject.toml` (`requires-python>=3.12`), and a `__main__` startup guard.
- WAL-retry consistency (`WalConversionError`), FK index
  `idx_inbox_from_agent`, version regex `\d{4,}` + int-sort, pytest 9.0.3,
  ruff `server/tests/**` ignore glob, docstring/README notes.

### Path A rubber-duck follow-ups ✅ FIXED

- **Authorizer permitted ATTACH/DETACH** (Blocking): an attached database is
  outside the runner transaction's atomicity boundary (and ATTACH can create an
  external file an outer ROLLBACK cannot clean up). The authorizer now also
  denies `SQLITE_ATTACH`/`SQLITE_DETACH`; added an `attach`-body test case.
- **`install.ps1` PATH-only `python` bug** (Blocking): the
  `$pythonBootstrap[1..($len-1)]` slice produced a descending `1..0` range when
  only `python` (no `py`) was on PATH, mis-invoking the bootstrap. Replaced with
  `Select-Object -Skip 1`.
- **Apply-phase WAL retry coverage** (Minor): the existing WAL-retry test only
  exercised the bootstrap path; added
  `test_apply_migration_retries_on_wal_conversion_error` that isolates the
  failure to `_apply_migration`'s own connection setup.
- **Misleading "locked" retry log** (Minor): generalized both retry messages to
  "database busy or WAL setup contended" since `WalConversionError` is also
  retried here.

**Result:** 74 tests pass; `ruff check` + `ruff format --check` clean.

### Path A final 8-reviewer round (4 roles × 2 models, fresh sessions) ✅ ADDRESSED

8 reviewers (Backend, QA, Systems, Migration — Claude + GPT-5.5) re-reviewed the
full uncommitted diff in clean sessions and empirically re-verified the
atomicity/authorizer/retry core. **No CRITICAL.** Verdict: sound and ship-able.
All MEDIUM + LOW addressed; nits deferred.

- **MEDIUM — Python guard shadowed by version-specific imports** (`__main__.py`):
  moved the `<3.12` guard above `from datetime import UTC` so a stale interpreter
  gets the actionable message, not an ImportError. Added `E402` per-file-ignore.
- **MEDIUM — Retry budget headroom** (`migrations/__init__.py`): `_MAX_RETRIES`
  5→8 for high-fan-out cold starts.
- **MEDIUM — Migration integrity guards** (`run_migrations`): fail loud on a
  missing applied-version file and on a pending version numbered below the
  highest applied version (silent out-of-order/divergence). Added two tests.
- **MEDIUM — Test hardening:** `db.py` WAL tests now assert the
  `WalConversionError` subtype (not base `RuntimeError`); added a file-based
  ATTACH test asserting the external file is never created; added a VACUUM
  rejection test; added a `detach` case to the rejection parametrization.
- **LOW:** structural `SQLITE_AUTH` errorcode check alongside the substring
  match; reworded the authorizer docstring (statements before the forbidden one
  do execute, then roll back); `f.is_file()` guard in `_discover_migrations`;
  `SQLITE_LOCKED`-as-retryable justification comment; documented the
  `in_transaction` branch as unreachable-by-design.

#### Decision — `idx_inbox_from_agent` stays in `0001_init.sql` (checksum)

Both migration reviewers flagged that editing the already-applied `0001_init.sql`
changes its checksum and would hard-fail startup for any DB that applied the old
version. **Conscious sign-off (pre-release):** `0001` is still the
in-development init schema, no persistent/production DB exists, and the README
already documents wiping the dev DB on a checksum mismatch. After v1 ships, schema
changes MUST go in a new `NNNN_*.sql` file rather than editing `0001`.

**Result:** 79 tests pass; `ruff check` + `ruff format --check` clean.
