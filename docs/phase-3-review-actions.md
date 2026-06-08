# Phase 3 — Review Actions (living record)

Mirror of `docs/phase-2-final-review-handover.md`: the full-scale review round
for Phase 3 (agent identity layer) and what was done about each finding.

## Round 1 — 8 fresh-session reviewers (2026-06-08)

Ran against the committed Phase 3 tree (`a5a293e`). Reviewer set (4 default /
4 GPT-5.5 for spread): `architect`, `backend-developer`, `database-engineer`,
`systems-engineer`, `qa-engineer`, `security-engineer`, plus `code-review` and
`rubber-duck`.

**Verdict: 7 SHIP / 1 DO-NOT-SHIP.** The single DO-NOT-SHIP (backend) and the
one substantive cross-reviewer finding was the **`add_alias` move/rename
recovery gap** (flagged independently by `backend` and `rubber-duck`,
corroborated by `code-review`). Everything else was hardening, contract
clarity, and test coverage. Nick's standing instruction: fix ALL findings,
including 🟢 LOW. Done below.

### 🔴/🟠 High

| # | Finding (reviewer) | Resolution |
|---|---|---|
| 1 | `add_alias` canonicalizes the **source** `agent_cwd` strictly, so a moved/renamed repo whose old path is gone cannot be used as the source — breaks the "moves just add an alias row" promise (backend, rubber-duck, code-review). | `canonicalize_agent_cwd` gained `require_exists: bool = True`. `add_alias` now canonicalizes the **source** in lookup mode (`require_exists=False`) — it must still match a stored alias row, so it cannot mint a phantom — while `new_cwd` stays strict. Tool docstring teaches "call add_alias(old, new) BEFORE re-registering from the new path." Test: `test_moved_source_path_still_resolves`, plus `test_lookup_mode_allows_missing_path`. |
| 2 | Concurrency convergence of `register_agent` untested (qa). | Added `test_concurrent_first_registration_converges_to_one_agent`: a peer holds `BEGIN IMMEDIATE` with the agent+alias staged; the loser's patched retry/backoff sleep commits the peer, then the loser retries, re-reads the alias, and converges to ONE agent (asserts the race was actually exercised). |

### 🟡 Medium

| # | Finding (reviewer) | Resolution |
|---|---|---|
| 3 | `db` ↔ `migrations` circular dependency hidden behind lazy imports; startup orchestration lives in the low-level layer (architect). | Moved `init_db` out of `db.py` into a new `np_agent_memory/startup.py`. Dependency direction is now a clean DAG: `startup → {db, migrations}`, `migrations → db`. `__main__` imports `init_db` from `startup`. `init_db` tests moved to `test_startup.py`. |
| 4 | `describe_agent` has two failure channels (soft `{registered: False}` vs raised `ValueError` from canonicalization) but the docstring advertises only the soft path (architect). | Documented both channels explicitly in `describe_agent` + `agent_describe` docstrings. Behavior unchanged (keeps typo protection). |
| 5 | Blank/whitespace `name` accepted and rewritten on every register — can clobber a good label (backend). | Added `_validate_metadata`: rejects blank/whitespace `name`. Test: `test_blank_name_rejected`. |
| 6 | Index `idx_inbox_to_unread` misleading — its columns are `(to_agent_id, acked_at, sent_at)`, i.e. *unacked* not *unread* (database). | Renamed to `idx_inbox_to_unacked` in `0001_init.sql` and `docs/PLAN.md`. **Edits 0001 → checksum change → dev/prod DB must be wiped** (see Clean-install note below). |
| 7 | `priority` stored as text sorts alphabetically, not by importance — forward-looking for Phase 4+ list tools (database). | No Phase 3 code change (only counts/filters today). Documented the caveat at the inbox/todo schema in `docs/PLAN.md`: phase 4+ list tools MUST map priority to an ordinal when ordering. |
| 8 | Self-asserted `agent_cwd` permits same-user impersonation (security). | Documented as an accepted trust assumption in `tools/agents.py` module docstring and `docs/PLAN.md` (identity model). `agent_cwd` is a routing key, not authentication; revisit before multi-user / cross-machine / privileged use. |
| 9 | Test gaps: exhaustive todo/blocker status filters, symlink collapse, `created_at` immutability (qa). | Added `test_open_todo_status_filter_is_exhaustive` (parametrized), `test_active_blocker_status_filter` (parametrized), `test_symlink_resolves_to_target` (skips on Windows symlink-privilege error), `test_reregister_preserves_created_at`. |

### 🟢 Low

| # | Finding (reviewer) | Resolution |
|---|---|---|
| 10 | Migration runner imports private `db._configure_connection` (architect). | Promoted to public `configure_connection`; updated `migrations` import + `test_db.py` / `test_migrations.py`. |
| 11 | Inner `ROLLBACK` in `_run_in_txn` unguarded — can mask the original error on auto-rollback codes (architect, code-review). | Wrapped the inner rollback in `if conn.in_transaction: with suppress(sqlite3.OperationalError): ...`, mirroring the outer handler. |
| 12 | Stale "Phase 2 scope" docstring headers on Phase-3-active modules (architect). | Rewrote `db.py` and `__main__.py` module docstrings to be phase-agnostic. |
| 13 | `Path.resolve()/.exists()` on caller input can raise `OSError` (UNC/permission) (security). | `canonicalize_agent_cwd` wraps filesystem calls in `try/except OSError` → clean `ValueError`. |
| 14 | Unbounded `name`/`workstream`/`description`/`agent_cwd` (security). | Added server-side caps: `name`/`workstream` ≤ 128, `description` ≤ 4096, `agent_cwd` ≤ 4096. Tests: `test_overly_long_name_rejected`, `test_overly_long_description_rejected`, `test_rejects_overly_long_path`. |
| 15 | Stored metadata echoed raw — future consumers must treat as untrusted (security). | Documented in `tools/agents.py` docstring + `docs/PLAN.md`. No code change (raw store/return is expected for a memory tool). |
| 16 | `describe_agent` metadata fields never asserted (qa). | Added `test_registered_returns_metadata_fields` (asserts workstream/description/canonical_path and no ID leak). |
| 17 | `blockers unique(agent_id, external_key)` nullable-unique — confirm intentional (database). | Confirmed intentional (NULL external_key blockers coexist; externally-keyed ones dedupe). No change; already commented in SQL. |
| 18 | Tool helpers don't retry `WalConversionError` from `open_connection`, but `init_db` runs at startup before tools serve (systems). | No change — not reachable in practice (WAL is persistent once `init_db` sets it). Noted for the record. |

## State after Round 1

- Tests: **121 passed** (was 103; +18). `ruff check` / `ruff format --check`
  clean. Run from repo root:
  `$env:PYTHONPATH="$(Get-Location)\server"; .\.venv\Scripts\python.exe -m pytest server\tests\ -q`
- New modules: `np_agent_memory/startup.py`, `tests/test_startup.py`.
- ADR 0002 (`docs/decisions/0002-cross-agent-addressing-public-handle.md`)
  written this session — reviewers validated its reasoning (architect).

## ⚠️ Clean-install note — DB wipe required

Finding #6 renamed an index inside `0001_init.sql`, changing its SHA-256
checksum. The migration runner treats a changed checksum on an
already-applied DB as fatal ("shipped migration modified"). Pre-release this is
fine, but the existing runtime DB MUST be deleted before the next server start:

```powershell
Remove-Item "$HOME\.copilot\np-agent-memory\agent-memory.db*" -ErrorAction SilentlyContinue
```

Tests are unaffected (fresh temp DB per run). Do this as part of the Phase 3
clean-install step.
