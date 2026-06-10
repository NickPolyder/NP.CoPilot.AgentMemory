# Phase 12 — One-time backfill of historical handover markdown (optional)

### 🔄 Handoff: np-agent-memory implementing agent → Connects agent (or ops)

**Reason:** Before the DB-backed transport existed, handovers were markdown
files under `follow-ups\handovers\processed\`. To get a complete history in the
shared store, import those files into `handovers` (+ derived `notes`) once. This
reads files in the Connects/journal tree, so it belongs to that agent.

**Priority:** Advisory / optional — only do this if a complete historical
timeline in `np-agent-memory` is wanted. New handovers already flow through
`handover_save`; this is purely catch-up.

**Depends on:** Phase 10 (producers cut over to `handover_save`) so the backfill
doesn't race live writes.

---

## Context

- Historical handover markdown lives in `follow-ups\handovers\processed\` (and
  possibly an unprocessed sibling). Each file is one session's handover for one
  agent, in the structured-body shape phase 10 preserves.
- The plan calls for the import to sit behind an explicit **`--backfill` flag**
  so it never runs by accident.

## Request

Write a one-time, idempotent backfill that, for each historical handover file:

1. Determines the **owning agent's canonical repo root** (`agent_cwd`) — from the
   file's front-matter / path / naming convention. The agent must already be
   registered (`agent_register`) so the path resolves; register it if the
   mapping is known.
2. Calls
   `np-agent-memory-handover_save(agent_cwd=<root>, summary=<first line / title>,
   body_md=<file body>, metadata={"backfill": true, "source_path": "<path>"})`.
3. Records which files were imported so a re-run skips them (idempotency).

Guard the whole thing behind a `--backfill` flag (no-op without it).

### Idempotency

`handover_save` always inserts a new row (it has no natural dedupe key), so the
**backfill script** owns dedupe: keep a manifest of imported source paths (or
move imported files to a `…/imported\` folder) and skip anything already done.
Stamp `metadata.source_path` on every saved row so the origin is traceable and a
future reconciliation can detect duplicates.

## Constraints / locked decisions

- **Behind `--backfill` only.** Default run does nothing.
- **Idempotent.** Re-running must not create duplicate handover rows — the
  script tracks imported files; do not rely on the server to dedupe.
- Run **after** phase 10 cutover so live `handover_save` writes and the backfill
  don't interleave for the same agent.
- Preserve original timestamps where possible (carry the file's date into
  `metadata`; `saved_at` is server-generated, so note the original date in the
  body/metadata if it matters for the timeline).

## Done when

- `… --backfill` imports every processed handover file exactly once, each as a
  `handovers` row tagged `metadata.backfill=true` + `source_path`.
- A second `--backfill` run imports nothing (clean no-op).
- Connects ingest (phase 9) then picks up the backfilled rows via claim/ack like
  any other handover.
