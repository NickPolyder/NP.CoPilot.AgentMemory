-- 0002_add_notes_deleted_at.sql: soft-delete support for the notes timeline.
-- Adds a nullable deleted_at marker so memory_delete can soft-delete (default)
-- without losing the row, while hard-delete still removes it. See ADR 0005.

alter table notes add column deleted_at text;

-- Partial index for the common "alive notes, newest first" read path so the
-- deleted_at IS NULL filter added to memory_query / memory_export stays cheap.
create index if not exists idx_notes_agent_time_alive
  on notes(agent_id, timestamp desc, id desc)
  where deleted_at is null;
