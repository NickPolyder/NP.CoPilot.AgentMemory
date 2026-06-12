# Phase 9 — Rewrite the Connects `ingest-handovers` skill (claim/ack model)

### 🔄 Handoff: np-agent-memory implementing agent → Connects agent

**Reason:** The handover transport moved from markdown files to the shared
`np-agent-memory` SQLite store. Connects must ingest handovers via the new
crash-safe claim → ack protocol instead of reading files. This touches files in
`C:\path\to\Connects`, which the np-agent-memory repo agent must not edit.

**Priority:** Advisory (do when convenient; old file-based ingest keeps working
during the dual-write transition).

---

## Context

- The `np-agent-memory` plugin is installed and exposes its tools under the
  `np-agent-memory-` prefix. The handover consumer tools are **server-scoped**
  (no `agent_cwd`): `handover_claim`, `handover_ack`, `handover_release`.
- Producers (every agent) now call `handover_save(...)`. Connects is the single
  **consumer** and ingests into `data/progress.db`.
- The protocol is two-phase so an ingest crash never loses data:
  `handover_claim` → write to `progress.db` → `handover_ack`. A claim older than
  `stale_minutes` is reclaimable by the next run.

## Request

Rewrite `.github/skills/ingest-handovers/SKILL.md` in the Connects repo to drive
this loop, and add the idempotency schema to `data/progress.db`.

### 1. The ingest loop the skill must implement

```text
1. batch = handover_claim(consumer_id="connects-ingest", limit=50)
2. for each h in batch.handovers:
     - upsert into progress.db (see schema below) using
         source_system = "np-agent-memory"
         source_table  = "handovers"
         source_id      = h.id
       The UNIQUE(source_system, source_table, source_id) constraint makes a
       retry a no-op (INSERT ... ON CONFLICT DO NOTHING / UPSERT).
3. handover_ack(consumer_id="connects-ingest", ids=[h.id for h in batch.handovers])
4. Repeat while batch.count == limit (more may remain).
5. On any failure mid-batch, call
     handover_release(consumer_id="connects-ingest", ids=[...], last_error="...")
   so the rows are retried next run instead of waiting out the stale timeout.
```

Use the **same `consumer_id`** (`"connects-ingest"`) for claim, ack, and release
— ack/release only affect rows currently claimed by that id.

### 2. Fields returned by `handover_claim`

Each claimed handover is shaped:

```json
{
  "id": "<ULID>",
  "agent_id": "<internal ULID>",
  "agent_name": "backend-developer",
  "session_id": "<optional>",
  "saved_at": "<UTC ISO-8601>",
  "summary": "one-line summary",
  "body_md": "full structured markdown body",
  "attempt_count": 1,
  "claimed_at": "<UTC ISO-8601>",
  "claimed_by": "connects-ingest",
  "metadata": { ... } | null
}
```

`body_md` is the **full** body (never truncated for the consumer). Map
`summary` + `body_md` into a `daily_notes` row (and derive `blockers` /
`reminders` rows if your existing parsing does that).

### 3. Schema changes to `data/progress.db`

Add `source_*` columns + a uniqueness constraint to the ingest target tables so
retries are idempotent:

```sql
-- daily_notes (and analogously: blockers, reminders)
alter table daily_notes add column source_system text;
alter table daily_notes add column source_table  text;
alter table daily_notes add column source_id      text;

create unique index idx_daily_notes_source
  on daily_notes(source_system, source_table, source_id)
  where source_system is not null;
```

(A partial unique index keeps existing rows with NULL `source_*` valid.)

## Constraints / locked decisions

- **Two-phase ack only** — never a single-call "read + mark consumed". The
  claim/ack/release split is the crash-safety contract; do not collapse it.
- **Idempotent writes** — every insert keyed on
  `(source_system, source_table, source_id)`; a re-ingest must be a no-op.
- The dashboard, link patterns, and existing `progress.db` tables stay
  unchanged. Only add the `source_*` columns + index.
- Do **not** delete the markdown handover files during the dual-write
  transition. Backfill of historical files is a separate task (phase 12).

## Done when

- `ingest-handovers` runs the claim → write → ack loop against
  `np-agent-memory-handover_*` tools.
- `progress.db` has the `source_*` columns + partial unique indexes.
- Re-running ingest with no new handovers is a clean no-op (0 inserts).
- A simulated crash between claim and ack leaves the rows reclaimable (verify by
  claiming, not acking, waiting past `stale_minutes`, and re-claiming).
