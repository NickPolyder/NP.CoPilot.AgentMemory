---
name: agent-memory
description: >
  Persistent cross-session memory, long-running todos, blockers, a cross-agent
  inbox, and session handovers for every Copilot CLI agent — backed by the
  shared np-agent-memory SQLite store. Use it to register yourself at session
  start, recall what past sessions did, log progress/decisions, track work that
  outlives a session, message other agents, and hand off cleanly at session end.
tags:
  - memory
  - continuity
  - inbox
  - handover
  - agent-coordination
visibility: user
tools:
  [agent]
---

# Purpose

`np-agent-memory` gives you a durable memory that survives session restarts and
a structured channel to coordinate with other agents on this machine. It is the
antidote to cold starts: instead of re-deriving context every session, you read
your own timeline, todos, and blockers, and pick up where you left off.

Everything is keyed to **your working directory**. The server cannot see your
cwd (inside a plugin-launched stdio process `os.getcwd()` is the plugin install
dir, not your repo), so **every agent-scoped tool takes an explicit `agent_cwd`
argument**. Get this right once at session start and reuse the value verbatim.

> **Tool names are namespaced.** In the CLI the tools appear as
> `np-agent-memory-agent_register`, `np-agent-memory-memory_log`, etc. This skill
> uses the bare names (`agent_register`, `memory_log`) for readability.

---

# The `agent_cwd` contract (read this first)

`agent_cwd` must be your **canonical repository root**, identical across every
call in the session and across sessions. The server canonicalizes it (absolute →
resolve symlinks → normalize Windows case → strip trailing separators →
forward-slash) and looks it up in `agent_aliases` to find *which agent you are*.

- **Git-backed agents:** use the repository root from
  `git rev-parse --show-toplevel`.
- **Non-git agents:** use a stable, absolute per-workstream root that never
  changes between sessions.

If a tool returns *"agent_cwd is not registered … Call agent_register first"*,
your path is wrong or you skipped registration — fix the path, do not invent a
new one (that mints a duplicate identity).

## Session-start snippet (copy-paste, idempotent)

On your **first turn** of every session:

```powershell
# Resolve the canonical root (git-backed agents):
$agentCwd = git rev-parse --show-toplevel
```

Then register yourself once (safe to call every session — it's idempotent and
never clears fields you omit):

```text
agent_register(
  agent_cwd  = "<the path above>",
  name       = "backend-developer",          # optional; see note below
  workstream = "np-agent-memory",            # optional grouping label
  description = "Implements the MCP server"   # optional, one line
)
```

> **Naming:** if you omit `name`, the server defaults it to your working
> directory's own name (e.g. `NP.CoPilot.AgentMemory`) on first registration.
> Prefer that default — show it to the user and ask whether they want a
> different name before overriding it. On later calls, omitting `name` keeps
> whatever is stored (it won't reset to the directory name), so only pass
> `name` when the user wants to change it.

Then orient yourself before doing work:

```text
agent_describe(agent_cwd)        # open_todos / active_blockers / unread_messages
memory_query(agent_cwd, limit=20)  # your recent timeline, newest first
inbox_check(agent_cwd, limit=20)   # messages other agents left you
handover_latest(agent_cwd)         # your last session's handover, if any
```

If you moved/renamed the repo, call `agent_add_alias(old_path, new_path)`
**before** re-registering from the new path, so the two paths resolve to the
same identity instead of splitting it.

---

# Tool reference

All agent-scoped tools take `agent_cwd` as the first argument. List/query tools
**require** a `limit` (server-capped at 200, default sizing 50); they return a
`next_cursor` you pass back as `cursor` to page. Long text fields come back
truncated — pass `full=true` to get the untruncated value.

## Identity

| Tool | What it does |
|------|--------------|
| `agent_register(agent_cwd, name?, workstream?, description?)` | Register/refresh you for this repo root. Idempotent. `name` defaults to the directory name on first call; omit it later to keep the stored name. |
| `agent_describe(agent_cwd)` | Your metadata + counts of open todos, active blockers, unread messages. |
| `agent_add_alias(agent_cwd, new_cwd)` | Attach a second path to the same identity (work-tree, moved repo). |
| `agent_list(limit, cursor?, workstream?, full?)` | **Global** directory of all registered agents (no `agent_cwd`) — find peers to address with `inbox_send`. Newest first; `description` truncated unless `full=true`; optional exact-match `workstream` filter. |

## Memory (durable timeline)

| Tool | What it does |
|------|--------------|
| `memory_log(agent_cwd, category, content, topic?, related_type?, related_id?, session_id?, metadata?)` | Append one note. `category` ∈ `progress` / `decision` / `note`. |
| `memory_query(agent_cwd, limit, category?, topic?, since?, cursor?, full?, include_deleted?)` | List notes newest-first, with filters + keyset pagination. Soft-deleted notes are hidden unless `include_deleted=true`. |
| `memory_export(agent_cwd, limit, category?, topic?, since?, cursor?, include_deleted?)` | Render a window of notes as markdown (grouped day → category) for human reading. Soft-deleted notes are omitted unless `include_deleted=true` (then marked _(deleted)_). |
| `memory_delete(agent_cwd, ids, hard?)` | Delete your own notes. **Soft by default** (`hard=false`): reversible, hides them from query/export but keeps the row — undo with `memory_restore`. `hard=true` is **permanent and irreversible** — only pass it after the user has explicitly confirmed they want the notes destroyed. |
| `memory_restore(agent_cwd, ids)` | Undo a **soft** delete — clears `deleted_at` so the notes reappear. Cannot recover hard-deleted notes. |

## Todos (work that outlives a session)

| Tool | What it does |
|------|--------------|
| `todo_add(agent_cwd, title, description?, priority?, due_date?, metadata?)` | Create a todo (status starts `pending`). `priority` ∈ `low`/`normal`/`high`/`urgent`. |
| `todo_list(agent_cwd, limit, status?, priority?, sort?, cursor?, full?)` | List todos. `sort` = `recent` (default) or `priority`. |
| `todo_update(agent_cwd, todo_id, status?, priority?, due_date?, description?)` | Change a todo. `status="done"` stamps `completed_at`. Statuses: `pending`/`in_progress`/`done`/`blocked`/`cancelled`. |

## Blockers (persistent impediments)

| Tool | What it does |
|------|--------------|
| `blocker_open(agent_cwd, title, description?, owner?, workstream?, external_key?)` | Open a blocker (status `active`); also auto-logs a related note. Pass `external_key` for idempotency. |
| `blocker_list(agent_cwd, limit, status?, workstream?, cursor?, full?)` | List blockers. `status` ∈ `active`/`escalated`/`resolved`. |
| `blocker_resolve(agent_cwd, blocker_id, resolution?)` | Resolve a blocker; auto-logs a note. |

## Inbox (agent-to-agent messages)

| Tool | What it does |
|------|--------------|
| `inbox_send(agent_cwd, to, subject, body, priority?, metadata?)` | Send to another registered agent. `to` = their canonical path **or** unique agent name. |
| `inbox_check(agent_cwd, limit, include_read?, cursor?, full?)` | List your unacked messages, newest-first. |
| `inbox_ack(agent_cwd, message_ids, status?)` | Mark messages `read` or `acked` (default `acked`). |

## Handovers (session-to-session, agent-side)

| Tool | What it does |
|------|--------------|
| `handover_save(agent_cwd, summary, body_md, session_id?, metadata?)` | Save a full session handover. Replaces writing a markdown file. |
| `handover_latest(agent_cwd, full?)` | Your most recent handover (or null). |
| `handover_export(agent_cwd, handover_id?)` | Render a handover (yours, or your latest) as full markdown. |

## Handover ingest (consumer-side — Connects only)

These are **not** for normal agents. The Connects `ingest-handovers` job uses a
crash-safe claim → ack protocol:

| Tool | What it does |
|------|--------------|
| `handover_claim(consumer_id, limit, stale_minutes?)` | Claim a batch of unconsumed handovers (stamps `claimed_by`). |
| `handover_ack(consumer_id, ids)` | Mark claimed handovers consumed (after they're safely stored). |
| `handover_release(consumer_id, ids, last_error?)` | Release claims for retry without waiting for the stale timeout. |

## Server-scoped

| Tool | What it does |
|------|--------------|
| `memory_backup_now()` | Force an online SQLite backup now (no `agent_cwd`). Backups otherwise run lazily once per day. |
| `memory_alive()` | Diagnostic ping; confirms the server loaded. |

---

# What to log — and what to skip

The `notes` stream is an **append-mostly** timeline: log freely and keep it
high-signal so future-you can skim it. Notes are not edited in place; to retract
one, use `memory_delete` (soft by default — see below). Pick the category by
intent:

| Category | Log this | Examples |
|----------|----------|----------|
| `progress` | A meaningful step forward worth recalling next session | "Finished phase 7 backup machinery; 78 tests green." |
| `decision` | A choice + its *why*, so it isn't re-litigated | "Chose stdio over long-lived backend — see ADR 0001. Per-call connections." |
| `note` | Durable context, gotchas, known-broken paths | "WAL conversion raises SQLITE_BUSY ignoring busy_timeout; use retry/backoff." |

**Do log:** non-obvious decisions and their rationale, surprising findings,
verified commands, dead ends (so you don't repeat them), and end-of-step
progress.

**Don't log:** routine tool output, things obvious from the code or git history,
ephemeral task chatter, or anything you'd never want to read again. Stateful
facts ("I opened a blocker", "I closed a todo") don't need a manual note — the
`blocker_*` / `todo_*` tools already record those, and blockers auto-log a
related note for you.

Use `topic` to tag a sub-area (e.g. `"migrations"`, `"phase-8"`) so you can
filter later with `memory_query(category=…, topic=…)`. Use `related_type` /
`related_id` to link a note to a `todo`, `blocker`, `pr`, etc.

## Deleting notes

To retract a note, call `memory_delete(agent_cwd, ids, hard?)` with ids from
`memory_query`. You can only delete your **own** notes.

- **Soft delete (default, `hard=false`)** is reversible: it hides the note from
  `memory_query` / `memory_export` but keeps the row. Pass `include_deleted=true`
  to either tool to still see soft-deleted notes (export marks them _(deleted)_),
  and call `memory_restore(agent_cwd, ids)` to bring them back. Prefer soft
  almost always.
- **Hard delete (`hard=true`)** permanently removes the row and **cannot be
  undone** (not even by `memory_restore`). Only pass `hard=true` after the user
  has **explicitly confirmed** they want the notes destroyed — never decide to
  hard-delete on your own.

---

# Good inbox patterns

The inbox is for **handing another agent something actionable**, not chit-chat.

- **Address by name when you know it** (`to="frontend-developer"`), or by their
  canonical path. Names must be unique to resolve.
- **One concern per message.** A focused subject + body beats a bundled wall.
- **Make the body self-contained** — the recipient won't see your conversation.
  State what you need, the context, and any links/ids. Mirror the agent-handoff
  shape (Reason / Context / Request / Artifacts / Constraints / Priority) when
  it's a real handoff.
- **Set `priority`** honestly (`urgent` only when it truly blocks them).
- **On the receiving side:** `inbox_check` at session start, act, then
  `inbox_ack` so it doesn't resurface. Use `status="read"` to keep it visible
  but mark it seen; `status="acked"` to archive it.

---

# Reading vs. exporting

- Use **`*_query` / `*_list` / `*_check`** when *you* (the agent) need the data
  to reason — they're paginated and truncate long bodies (pass `full=true` for
  the whole field).
- Use **`memory_export` / `handover_export`** when you want **human-readable
  markdown** to show the user or paste into a doc — they return full, formatted
  content.

---

# End-of-session handover

When you wrap substantial work, save a handover instead of writing a markdown
file. Keep the structured body shape the team already uses:

```text
handover_save(
  agent_cwd = <your canonical root>,
  summary   = "Phase 8: bundled agent-memory skill written; tests green.",
  body_md   = """
## What I did
- …

## Current state
- …

## Next steps
- …

## Decisions / notes
- …

## Open questions / blockers
- …
""",
  session_id = <optional>
)
```

Connects ingests saved handovers automatically via the claim/ack protocol — you
don't need to do anything beyond `handover_save`. To re-read your last one,
`handover_latest(agent_cwd)`; to render it for a human, `handover_export`.
