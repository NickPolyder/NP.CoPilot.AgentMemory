# Agent Purge Lifecycle

## Purpose

Registered agents need an explicit lifecycle so obsolete identities can be
removed without immediately destroying their durable data.

## Behavior

`agent_purge` is a global lifecycle action addressed by a registered
`target_cwd`.

- It requires `confirm=true` after the user has explicitly requested or
  confirmed the selected mode.
- The default is a reversible soft purge: the agent receives `deleted_at`, and
  all agent-scoped and global discovery surfaces hide it and its data.
- `agent_list(show_deleted=true)` is the only inspection path for soft-deleted
  agents, and marks them with `deleted_at`.
- `agent_unpurge` explicitly restores a soft-deleted agent and its preserved
  data. Normal registration never revives it implicitly.
- `hard=true` permanently removes the agent, aliases, notes, todos, blockers,
  inbox messages in either direction, and handovers in one write transaction.

## Visibility Boundary

Soft deletion is enforced at the shared identity resolver, recipient lookup,
inbox display and unread counts, plus handover claim and dead-letter listing.
This means a soft-deleted agent cannot act through agent-scoped tools, cannot
be addressed by peers, and does not appear in consumer-facing handover data.

## Recovery

Soft-purged rows remain intact until an explicit hard purge. Hard purges are
irreversible and `agent_unpurge` reports them as not found.

## Backup Retention

Hard purge removes data from the live database immediately. Existing daily
SQLite backups can retain the data until a later retention-pruning run deletes
the corresponding snapshot.
