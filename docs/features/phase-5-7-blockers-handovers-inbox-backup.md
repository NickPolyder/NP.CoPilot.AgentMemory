# Phases 5–7 — Blockers, Handovers, Inbox, Backup

Status: **done ✅**. Implements the remaining agent-scoped transport/persistence
tools plus the throttled backup machinery on top of the `blockers`, `handovers`,
`inbox`, and `backup_runs` tables already created by `0001_init.sql` (no new
migration required). Full suite **228 passing**, ruff check + format clean.

## Scope

### Phase 5 — Blockers + Handovers (`tools/blockers.py`, `tools/handovers.py`)

| Tool | Table | Kind | Summary |
|---|---|---|---|
| `blocker_open` | `blockers` | write | Raise a blocker (`summary`, `detail`, `severity`, `external_key`). Auto-logs a related note. Idempotent per agent on `external_key`. |
| `blocker_list` | `blockers` | read | Keyset-paginated list (filter: `status`). Truncates `detail` unless `full=true`. |
| `blocker_resolve` | `blockers` | write | Resolve a blocker (`resolution`); manages `resolved_at`. Raises on double-resolve / not-found. |
| `handover_save` | `handovers` | write | Persist a session handover (`title`, `body_md`, `metadata`). Agent-scoped. |
| `handover_latest` | `handovers` | read | Latest handover(s) for the calling agent. Truncates `body_md` unless `full=true`. |
| `handover_export` | `handovers` | read | Render a window of the calling agent's handovers as markdown (full body). |
| `handover_claim` | `handovers` | write | **Consumer-side** (`consumer_id`, cross-agent). Claim oldest unclaimed/stale handovers; stamps `claimed_at`/`claimed_by`, bumps `attempt_count`. |
| `handover_ack` | `handovers` | write | **Consumer-side**. Mark claimed rows `consumed_at` (only rows this consumer claimed). |
| `handover_release` | `handovers` | write | **Consumer-side**. Clear a claim + record `last_error` for clean backoff. |

### Phase 6 — Inbox (`tools/inbox.py`)

| Tool | Table | Kind | Summary |
|---|---|---|---|
| `inbox_send` | `inbox` | write | Send a message to another agent (recipient by canonical path or unique name). |
| `inbox_check` | `inbox` | read | List the calling agent's messages (default: unacked + unread; `include_read` drops the read filter). |
| `inbox_ack` | `inbox` | write | Mark messages `read` (sets `read_at`) or `acked` (sets `acked_at`, coalesces `read_at`). |

### Phase 7 — Backup (`backup.py`)

| Tool / fn | Kind | Summary |
|---|---|---|
| `memory_backup_now` | tool (server-scoped) | Force an online SQLite backup now + prune by retention. |
| `start_lazy_daily_backup()` | startup hook | Daemon thread, off the critical path; `maybe_daily_backup` throttles to ≤1/day via `backup_runs`. |
| `run_backup` / `maybe_daily_backup` / `prune_backups` | internal | Online backup via `sqlite3.Connection.backup()` (not file copy), 24h throttle, retention prune. |

## Conventions (shared with Phase 4)

- **Module layout** — one module per domain exposing `register_<domain>_tools(mcp)`,
  wired via `tools/__init__.register_all_tools`. Pure DB-logic functions take a
  `sqlite3.Connection`; thin `@mcp.tool` wrappers open a per-call connection.
  (`backup.py` lives at package root, not under `tools/`, but follows the same
  `register_*_tools` contract.)
- **Identity** — agent-scoped tools take explicit `agent_cwd`, canonicalized and
  resolved via `agent_aliases`; unregistered-but-valid paths raise. Agents never
  see internal ULIDs. **Exception:** consumer-side handover tools take a
  `consumer_id` string (cross-agent transport, not agent-scoped).
- **Resource IDs are visible** — blocker/handover/inbox ULIDs are returned and
  accepted. The "never expose IDs" rule is about *agent identity* only.
- **Transactions** — writes via `run_in_write_txn` (BEGIN IMMEDIATE + retry),
  reads via `run_in_read_txn`.
- **Required, server-capped `limit`** + opaque keyset (cursor) pagination
  (`limit + 1` look-ahead, `next_cursor`).
- **Body truncation** — `blocker_list` (`detail`), `handover_latest`/`claim`
  (`body_md`), and `inbox_check` truncate unless `full=true`; export renders full.
- **Embedded SQL keywords are UPPERCASE** — matches `memory.py`/`todos.py`/
  `_common.py`/`backup.py`. (Lowercase-SQL style applies to `.sql` migration
  files only.)

## Two-phase handover ack (hard rule)

Never single-call read+consume. `handover_claim` → process → `handover_ack`
(success) **or** `handover_release` (backoff). Claimable rows:
`consumed_at IS NULL AND (claimed_at IS NULL OR claimed_at < cutoff)`, oldest
first. Stale-claim cutoff = `now - stale_minutes` (default 15, capped 1440).
A consumer crash between claim and ack loses nothing — the claim simply ages out
and another consumer reclaims it.

## Verification

- Unit tests `tests/test_blockers.py`, `tests/test_handovers.py`,
  `tests/test_inbox.py`, `tests/test_backup.py` (happy path + validation +
  pagination + truncation + cross-agent isolation + claim/ack/release lifecycle +
  stale-claim reclaim + backup throttle/prune).
- `test_agents.test_registers_expected_tools` extended to assert the Phase 5/6/7
  tool names are registered.
- `ruff check` / `ruff format --check`, full `pytest` green (228).

## Follow-ups / notes

- `backup.py` carries a local `_now_iso` that duplicates `identity.now_iso`
  (cosmetic; could dedupe later).
- Phases 8–10 build on these: bundled skill (8), Connects `ingest-handovers`
  rewrite to the claim/ack model (9), `handover-report` skill rewrite (10).
