# Full review — findings & remediation backlog (2026-06-10)

Multi-model review of the implemented plugin (phases 0–8), run after pushing
`03df505`. Three independent reviewers covered the whole repo, review-only.

| Reviewer | Model | Verdict |
|----------|-------|---------|
| review-gemini | Gemini 3.1 Pro | No issues found |
| review-opus   | Claude Opus 4.8 | 1 Medium + 1 Low (backup) |
| review-gpt    | GPT-5.5 | 7 findings |

**Consensus on what's correct:** 228 tests pass; the transaction model
(per-call connections, WAL, `BEGIN IMMEDIATE`/`DEFERRED`, retry/backoff), keyset
pagination, SQL parameter binding, identity canonicalization, migration
atomicity/authorizer/checksums, and two-phase claim/ack ownership checks are all
sound. No SQL-injection vectors. Documented tool signatures match the code.

All findings below are **verified against the source**. None is a security hole
or data-loss bug; all are low blast radius.

---

## Priority set (do first)

### R1 — Automatic daily backups are never pruned · Medium
- **Where:** `server/np_agent_memory/backup.py:181-184` (and `112-147`).
- **Problem:** The lazy daily-backup thread (`start_lazy_daily_backup` →
  `maybe_daily_backup`) never calls `prune_backups()`. Retention runs **only**
  inside the manual `memory_backup_now` tool (`backup.py:210-211`). The automatic
  path produces one dated snapshot per day forever, so the documented 14-day
  retention (`docs/PLAN.md:114`) is silently unenforced → unbounded disk growth
  (~365 full DB copies/yr).
- **Reporters:** Opus + GPT.
- **Fix idea:** Call `prune_backups(backups_dir)` after a successful
  `maybe_daily_backup` inside the lazy-backup thread (best-effort, same
  try/except). Add a test asserting the automatic path prunes.

### R2 — Inbox responses leak internal agent ULIDs · Medium
- **Where:** `server/np_agent_memory/tools/inbox.py:98` (`from_agent_id`) and
  `inbox.py:161` (`to_agent_id`).
- **Problem:** Both are internal `agents.id` ULIDs returned to a normal agent,
  violating the "agents never see internal IDs" invariant
  (`server/np_agent_memory/tools/agents.py:3`). `from_label` already carries the
  human-readable sender name, so the ULID is redundant for display.
- **Reporter:** GPT (rated High; downgraded to Medium — ULIDs aren't secrets,
  but it's a genuine invariant breach).
- **Note:** `handover_claim` also returns `agent_id`
  (`tools/handovers.py:204-207`), but that path is **consumer-side** (Connects
  ingest) and arguably acceptable; decide explicitly whether to keep it.
- **Fix idea:** Drop `from_agent_id`/`to_agent_id` from the agent-facing inbox
  responses (keep `from_label`; expose a canonical path/name for the recipient
  if a handle is needed). Update `skills/agent-memory/SKILL.md` if the documented
  return shape changes.

### R3 — Forged cursors surface raw SQLite errors · Low/Medium
- **Where:** `server/np_agent_memory/tools/_common.py:69` (`decode_cursor`).
- **Problem:** `decode_cursor` only checks the decoded payload is a list; call
  sites only check length. A cursor whose elements are nested arrays/objects
  reaches SQLite parameter binding and raises `sqlite3.ProgrammingError` instead
  of a clean validation `ValueError`. Affects every paginated tool
  (`memory.py:153-172`, `todos.py:172-202`, `blockers.py:170-186`,
  `inbox.py:178-190`).
- **Reporter:** GPT.
- **Fix idea:** Validate cursor element types/arity per endpoint before building
  the keyset predicate (e.g. require str/int scalars). Add a test for a
  malformed-but-decodable cursor.

---

## Secondary (nice to have)

### R4 — `memory_export` returns an unusable pagination cursor · Low/Medium
- **Where:** `server/np_agent_memory/tools/memory.py:220-267`.
- **Problem:** `export_memory` returns `next_cursor` (:258-266) but neither the
  function nor the MCP tool signature accepts a `cursor` param (hardcoded
  `cursor=None` at :239). Once more than one page exists, callers can never fetch
  the next markdown page.
- **Reporter:** GPT.
- **Fix idea:** Either add a `cursor` param to `memory_export` and thread it into
  `_fetch_notes`, or stop returning `next_cursor`. Update
  `skills/agent-memory/SKILL.md` (which documents this field) accordingly.

### R5 — Installer can permanently reuse an unsupported (<3.12) venv · Low/Medium
- **Where:** `install.ps1:42-78`.
- **Problem:** Bootstraps with generic `py -3` / `python`, then reuses any
  existing venv (:56) **before** the 3.12+ check (:75). A first run on Python
  3.11 creates a venv that every rerun reuses and then rejects. It fails loud
  with guidance to recreate, but does not auto-heal.
- **Reporter:** GPT.
- **Fix idea:** Bootstrap with a 3.12+ interpreter explicitly, or auto-rebuild
  the venv when the version check fails (delete + recreate) rather than only
  throwing.

### R6 — Crashed backup suppresses backups for up to 24h · Low
- **Where:** `server/np_agent_memory/backup.py:123-137`.
- **Problem:** The throttle treats `finished_at IS NULL` rows as "in progress"
  and skips. A process killed between inserting the pending `backup_runs` row and
  `_finish_backup_run` leaves that row NULL permanently; daily backups are then
  skipped until the row ages past the 24h `started_at` window. Self-heals after
  24h.
- **Reporters:** Opus + GPT.
- **Fix idea:** Treat stale pending rows separately — only suppress on very
  recent pending rows, or mark abandoned pending rows failed before deciding to
  skip.

### R7 — Handoff doc ack example uses the wrong batch shape · Low (doc)
- **Where:** `docs/handoffs/phase-09-connects-ingest-rewrite.md` (ingest-loop
  step 3).
- **Problem:** Step 3 writes `ids=[h.id for h in batch]`, but `handover_claim`
  returns `{"handovers": [...], "count": N}` (`tools/handovers.py:271`); step 2
  already iterates `batch.handovers`. Following step 3 literally would fail to
  ack.
- **Reporter:** GPT.
- **Fix idea:** Change to `ids=[h.id for h in batch.handovers]` (or
  `batch["handovers"]`) for consistency.

---

## Suggested order for tomorrow

1. R1, R2, R3 (priority set) — small, contained, each gets a test.
2. R4 + R7 (touch `SKILL.md` / handoff doc — quick).
3. R5, R6 (installer + backup throttle hardening).

Each fix should keep the existing test suite green and add a focused regression
test where applicable. No fix is started yet — this backlog is the source of
truth for the remediation pass.
