-- 0001_init.sql: Initial schema for np-agent-memory.
-- Creates all tables, indexes, and constraints per docs/PLAN.md.

-- Agents: ULID primary key. Resolved server-side from canonicalized agent_cwd.
create table if not exists agents (
  id          text primary key,
  name        text not null,
  workstream  text,
  description text,
  created_at  text not null,
  updated_at  text not null
);

-- Canonicalized path(s) that resolve to an agent.
create table if not exists agent_aliases (
  alias_path text primary key,
  agent_id   text not null references agents(id) on delete cascade,
  created_at text not null
);

create index if not exists idx_agent_aliases_agent on agent_aliases(agent_id);

-- Append-only event stream.
-- ON DELETE RESTRICT: agents must be explicitly cleaned up before removal.
create table if not exists notes (
  id            text primary key,
  agent_id      text not null references agents(id) on delete restrict,
  timestamp     text not null,
  category      text not null
    check (category in ('progress', 'decision', 'note')),
  topic         text,
  content       text not null,
  session_id    text,
  related_type  text,
  related_id    text,
  metadata_json text
    check (metadata_json is null or json_valid(metadata_json))
);

create index if not exists idx_notes_agent_time on notes(agent_id, timestamp desc, id desc);
create index if not exists idx_notes_category   on notes(category, timestamp desc);
create index if not exists idx_notes_topic      on notes(topic, timestamp desc);
create index if not exists idx_notes_session    on notes(session_id);
create index if not exists idx_notes_related    on notes(related_type, related_id);

-- Long-running todos that span sessions.
create table if not exists todos (
  id            text primary key,
  agent_id      text not null references agents(id) on delete restrict,
  title         text not null,
  description   text,
  status        text not null default 'pending'
    check (status in ('pending', 'in_progress', 'done', 'blocked', 'cancelled')),
  priority      text not null default 'normal'
    check (priority in ('low', 'normal', 'high', 'urgent')),
  due_date      text,
  created_at    text not null,
  updated_at    text not null,
  completed_at  text,
  metadata_json text
    check (metadata_json is null or json_valid(metadata_json))
);

create index if not exists idx_todos_agent_status on todos(agent_id, status, priority);
create index if not exists idx_todos_due          on todos(agent_id, due_date);

-- Persistent blockers across sessions.
create table if not exists blockers (
  id            text primary key,
  agent_id      text not null references agents(id) on delete restrict,
  external_key  text,
  title         text not null,
  description   text,
  owner         text,
  workstream    text,
  status        text not null default 'active'
    check (status in ('active', 'escalated', 'resolved')),
  raised_at     text not null,
  escalated_at  text,
  resolved_at   text,
  resolution    text,
  unique (agent_id, external_key)
);

create index if not exists idx_blockers_agent_status on blockers(agent_id, status);
create index if not exists idx_blockers_workstream   on blockers(workstream, status);

-- Cross-agent inbox.
-- from_agent_id is nullable: allows system-generated messages with no sender.
create table if not exists inbox (
  id            text primary key,
  from_agent_id text references agents(id) on delete restrict,
  from_label    text,
  to_agent_id   text not null references agents(id) on delete restrict,
  subject       text not null,
  body          text not null,
  priority      text not null default 'normal'
    check (priority in ('low', 'normal', 'high', 'urgent')),
  sent_at       text not null,
  read_at       text,
  acked_at      text,
  metadata_json text
    check (metadata_json is null or json_valid(metadata_json))
);

create index if not exists idx_inbox_to_unread      on inbox(to_agent_id, acked_at, sent_at desc);
create index if not exists idx_inbox_to_unread_prio on inbox(to_agent_id, read_at, priority, sent_at desc);
create index if not exists idx_inbox_from_agent     on inbox(from_agent_id);

-- Handovers: two-phase claim/ack for safe Connects ingest.
create table if not exists handovers (
  id            text primary key,
  agent_id      text not null references agents(id) on delete restrict,
  session_id    text,
  saved_at      text not null,
  summary       text not null,
  body_md       text not null,
  claimed_at    text,
  claimed_by    text,
  attempt_count integer not null default 0,
  last_error    text,
  consumed_at   text,
  metadata_json text
    check (metadata_json is null or json_valid(metadata_json))
);

create index if not exists idx_handovers_claimable on handovers(consumed_at, claimed_at, saved_at);
create index if not exists idx_handovers_agent     on handovers(agent_id, saved_at desc);
create index if not exists idx_handovers_session   on handovers(session_id);

-- Backup throttle.
create table if not exists backup_runs (
  id          integer primary key autoincrement,
  started_at  text not null,
  finished_at text,
  path        text not null,
  success     integer not null default 0
);
