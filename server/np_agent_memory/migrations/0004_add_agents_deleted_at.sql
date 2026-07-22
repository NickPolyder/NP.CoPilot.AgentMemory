-- 0004_add_agents_deleted_at.sql: soft-delete support for agent identities.
-- Adds a nullable deleted_at marker so a purge can hide an agent and all
-- agent-scoped data without destroying the rows needed by agent_unpurge.

alter table agents add column deleted_at text;

-- Partial index for the normal active-only directory listing.
create index if not exists idx_agents_created_at_alive
  on agents(created_at desc)
  where deleted_at is null;
