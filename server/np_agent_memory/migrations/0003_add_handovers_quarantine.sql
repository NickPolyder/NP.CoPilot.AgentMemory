-- 0003_add_handovers_quarantine.sql: dead-letter support for handover ingest.
-- Adds a nullable quarantined_at marker so a handover that repeatedly fails
-- ingest (a release at/above the attempt cap) is moved to a terminal
-- dead-letter state instead of being retried forever. Quarantined rows are
-- excluded from handover_claim and surfaced via handover_quarantined for
-- consumer inspection. See ADR 0007.

alter table handovers add column quarantined_at text;

-- Partial index for the "list quarantined, newest first" inspection path so
-- the quarantined_at IS NOT NULL filter stays cheap as live rows grow.
create index if not exists idx_handovers_quarantined
  on handovers(quarantined_at desc, id desc)
  where quarantined_at is not null;
