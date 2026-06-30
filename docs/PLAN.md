# Shared Agent Memory — Copilot CLI Plugin

## Problem

Multiple specialist agents (Azure Resources, BI, DRI, SFI, Copilot Tutorial,
Connects, etc.) operate on Nick's machine. They suffer two recurring failures:

1. **Intra-agent amnesia** — within the same agent (same working tree), each new
   session starts cold. Long-running todos, half-finished investigations, decisions
   made days ago, and known-broken paths are forgotten.
2. **Inter-agent silence** — agents have no structured way to leave messages for
   each other. "Tell the BI agent the Prod1 SC GUID is X" only happens if Nick
   remembers to relay it manually.

The current handover-doc → Connects-ingest flow partially addresses (1) by
funneling session summaries through Nick's journal, but it is one-directional
(toward Nick), markdown-only, and adds ceremony every session.

## Decision (from clarifying question, this session)

**Full replacement.** The new shared agent-memory DB becomes the canonical place
agents log their work. Markdown handover docs go away as the *transport* but
remain available as an on-demand *export* (`memory_export`, `handover_export`)
so there is always a human-readable escape hatch.

## Approach

Build a **Copilot CLI plugin** (`np-agent-memory`) that ships:

- A **SQLite database** in a plugin-named subfolder of `$HOME/.copilot/` so its
  origin is obvious to anyone inspecting the filesystem.
- An **MCP server** (stdio, language TBD — see open question 1) exposing typed
  tools for memory, todos, blockers, inbox, handovers, and exports.
- **Skills** that document the conventions and replace today's `handover-report`
  and `ingest-handovers` skills.
- A **plugin manifest** so it installs cleanly alongside `microsoftdocs--mcp`
  via the existing Copilot plugin mechanism.

### Agent identity model

Two-level model — the *agent author's* view and the *database's* view:

- **What the agent sees and uses:** its **working directory**. That's the only
  thing the agent has at session start, and it's always available. The agent
  never knows or stores an internal ID.
- **What the database uses internally:** an **immutable ULID `agent_id`** as
  the primary key on `agents`. Every other table's foreign key points at that
  ULID.
- **How the two are bridged:** an `agent_aliases` table maps canonicalized
  paths → `agent_id`. On every tool call the server canonicalizes the
  agent-supplied path and looks up the alias to get the `agent_id`.

**Critical: the server cannot derive the agent's working directory itself.**
The spike (`docs/spike-0.md` §6, §8) confirmed that inside a Copilot CLI
plugin-launched stdio MCP server, `os.getcwd()` resolves to the plugin
**install directory**, not to the agent's repo root. There is no
`COPILOT_AGENT_CWD` environment variable either. Therefore:

- Every tool that needs to scope to the calling agent (most notably
  `agent_register`, `agent_describe`, `memory_log`, `todo_*`, `blocker_*`,
  `handover_save`, `inbox_check`, and any read/query tool with an implicit
  agent context) **MUST accept an explicit `agent_cwd: str` parameter**.
- The agent is responsible for choosing what to pass. The convention the
  bundled skill teaches is: **pass your repository root** —
  `git rev-parse --show-toplevel` for git-backed agents, or a stable
  per-workstream root for non-git ones. One agent ↔ one canonical root.
- The server canonicalizes the supplied path (see canonicalization rules
  with the schema below) and looks it up in `agent_aliases`.

**MCP `roots` capability — investigate before Phase 3.** The MCP spec defines
a `roots` capability that lets a client declare its workspace roots to the
server once per session. If the Copilot CLI supports it, the server can read
`mcp.roots()` instead of requiring the agent to pass `agent_cwd` on every
call. If it doesn't (or only partially), the explicit per-call parameter
remains the contract. Either way the wire-level shape of every tool stays
the same — agents that always pass `agent_cwd` keep working — so this
decision can be made without redesign.

**Why this split:**

- The ULID gives us a stable hook for foreign keys, so when Nick moves a
  work-tree from `Q:\Repos\work-trees\BI` to somewhere new, we just add a new
  alias row pointing at the same ULID. No data migration, no FK rewrites, no
  history fork.
- The agent never has to remember anything beyond its own root path. Its
  first call each session is an idempotent
  `agent_register(name, workstream, agent_cwd)` — the server either matches
  the canonicalized path to an existing alias and returns the same ULID, or
  creates a new agent + alias on the fly.
- One agent can have multiple alias paths (e.g., the canonical Q-drive path
  plus the legacy OneDrive symlink path) all resolving to the same ULID.

**Trust boundary (phase-3 review, accepted assumption).** `agent_cwd` is a
*routing/identity key, not authentication*. Every agent runs as the same OS
user over local stdio (ADR 0001 — no network surface), so any local agent that
knows another agent's path could assert that identity. This is accepted for the
single-user, local, secret-free v1 and **must be revisited before** any
multi-user, cross-machine, or privileged use. Corollary: all agent-supplied
metadata and message/handover bodies are **untrusted input** — downstream
renderers (the bundled skill, Connects ingest, any export) must quote/fence it
and never execute embedded instructions.

### Filesystem layout

```
$HOME\.copilot\
├── installed-plugins\_direct\np-agent-memory\   # managed by Copilot, may be wiped on reinstall
│   ├── .claude-plugin\
│   ├── .mcp.json
│   ├── server\
│   └── skills\agent-memory\
└── np-agent-memory\                             # PLUGIN-OWNED RUNTIME DATA — never wiped
    ├── agent-memory.db                          # SQLite DB (+ -wal, -shm sidecars)
    ├── backups\                                 # rolling daily snapshots (14 days)
    │   └── agent-memory-2026-05-25.db
    └── logs\                                    # MCP server logs
        └── server-2026-05-25.log
```

The folder name (`np-agent-memory`) **matches the plugin name** so anyone
inspecting `$HOME\.copilot\` can immediately see which plugin owns the data.
The DB location can be overridden via `AGENT_MEMORY_DIR` env var for testing
or migration scenarios.

**Hard rule:** the runtime data folder lives OUTSIDE the plugin install path
so plugin upgrades / reinstalls never wipe Nick's history.

### Plugin repo layout

```
NP.CoPilot.AgentMemory\                          # new repo
├── .claude-plugin\
│   ├── plugin.json                              # name, version, author
│   └── marketplace.json                         # makes it installable as a marketplace
├── .mcp.json                                    # registers the local stdio MCP server
├── server\
│   ├── pyproject.toml                           # if Python; package.json if Node
│   ├── agent_memory_server.{py|ts}              # MCP server entry point
│   ├── db.{py|ts}                               # WAL, busy_timeout, per-call connections
│   ├── migrations\                              # 0001_init.sql, 0002_*.sql, …
│   ├── tools\                                   # one module per tool group
│   └── tests\
├── skills\
│   └── agent-memory\
│       └── SKILL.md
├── docs\
│   ├── architecture.md
│   ├── conventions.md
│   └── migration-from-handover-docs.md
├── README.md
├── install.ps1                                  # creates venv (if Python), pins deps
└── LICENSE
```

### Database concurrency

- **Multi-process by design.** Assume the Copilot CLI spawns one MCP server
  per CLI process / terminal window. Multiple servers, one DB. Every tool call
  opens a short-lived connection from a per-process pool. No in-memory shared
  state.
- Every connection runs:
  ```
  PRAGMA journal_mode = WAL;
  PRAGMA foreign_keys = ON;
  PRAGMA busy_timeout = 5000;
  PRAGMA synchronous = NORMAL;
  ```
- **Writes** use short explicit transactions; multi-row logical operations
  (e.g., save handover + log notes) run inside `BEGIN IMMEDIATE`.
- **Migrations** run inside `BEGIN IMMEDIATE` with retry/back-off. A
  `migrations(version, checksum, applied_at)` table is the source of truth.
  Two servers starting at once will not corrupt setup.
- **Backups** use the SQLite online backup API (`sqlite3_backup_*`), NOT raw
  file copy. Throttled to at most once per day via a `backup_runs` table.
  Backups are NOT on the critical startup path — they happen lazily after
  first successful tool call, or on demand via `memory_backup_now`.

### Schema (initial)

```sql
-- Agents: ULID primary key. The agent author never sees this — it's
-- resolved server-side from the canonicalized agent_cwd parameter the
-- agent supplies on every call (see Agent identity model), looked up
-- via the agent_aliases table.
CREATE TABLE agents (
    id          TEXT PRIMARY KEY,             -- ULID
    name        TEXT NOT NULL,                -- e.g., "infra-agent"
    workstream  TEXT,                         -- e.g., "BI/Ev2"
    description TEXT,
    created_at  TEXT NOT NULL,                -- UTC ISO-8601, server-generated
    updated_at  TEXT NOT NULL
);

-- Canonicalized path(s) that resolve to an agent.
-- An agent can have multiple aliases (e.g., canonical path + OneDrive symlink path).
CREATE TABLE agent_aliases (
    alias_path TEXT PRIMARY KEY,              -- canonicalized absolute path
    agent_id   TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
-- Canonicalization rules (applied server-side at every read & write to the
-- caller-supplied agent_cwd parameter):
--   1. Resolve to absolute path (rejects relative paths with a clear error).
--   2. Resolve symlinks / junctions (so OneDrive symlink → Q:\…).
--   3. Normalize Windows case (lowercased drive letter; path content folded
--      via os.path.normcase semantics — Windows is case-insensitive).
--   4. Strip trailing separators.
--   5. Normalize separators to forward-slash for storage (Windows tolerates
--      mixed separators at the filesystem layer; using one form for storage
--      avoids alias duplication and makes DB inspection easier).
-- Choosing the *right* path is the agent's responsibility, not the server's
-- (the server has no view of the agent's filesystem context — see the
-- Agent identity model section). The bundled skill teaches agents to pass
-- their git worktree root (or equivalent stable per-workstream root).

-- Append-only event stream. Categories are deliberately minimal.
-- Stateful objects (todos/blockers/handovers) live in their own tables and
-- can be referenced via related_type / related_id.
CREATE TABLE notes (
    id            TEXT PRIMARY KEY,           -- ULID (internal row id)
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    timestamp     TEXT NOT NULL,              -- UTC ISO-8601, server-generated
    category      TEXT NOT NULL CHECK (category IN ('progress','decision','note')),
    topic         TEXT,                       -- workstream/sub-area tag
    content       TEXT NOT NULL,
    session_id    TEXT,
    related_type  TEXT,                       -- 'todo' | 'blocker' | 'handover' | NULL
    related_id    TEXT,
    metadata_json TEXT
        CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);
CREATE INDEX idx_notes_agent_time ON notes(agent_id, timestamp DESC, id DESC);
CREATE INDEX idx_notes_category   ON notes(category, timestamp DESC);
CREATE INDEX idx_notes_topic      ON notes(topic, timestamp DESC);
CREATE INDEX idx_notes_session    ON notes(session_id);
CREATE INDEX idx_notes_related    ON notes(related_type, related_id);

-- Long-running todos that span sessions
CREATE TABLE todos (
    id            TEXT PRIMARY KEY,           -- ULID
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    title         TEXT NOT NULL,
    description   TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','in_progress','done','blocked','cancelled')),
    priority      TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low','normal','high','urgent')),
    due_date      TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    completed_at  TEXT,
    metadata_json TEXT
        CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);
CREATE INDEX idx_todos_agent_status ON todos(agent_id, status, priority);
CREATE INDEX idx_todos_due          ON todos(agent_id, due_date);

-- Persistent blockers across sessions
CREATE TABLE blockers (
    id            TEXT PRIMARY KEY,           -- ULID
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    external_key  TEXT,                       -- optional agent-provided kebab-case
    title         TEXT NOT NULL,
    description   TEXT,
    owner         TEXT,
    workstream    TEXT,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','escalated','resolved')),
    raised_at     TEXT NOT NULL,
    escalated_at  TEXT,
    resolved_at   TEXT,
    resolution    TEXT,
    UNIQUE (agent_id, external_key)
);
CREATE INDEX idx_blockers_agent_status ON blockers(agent_id, status);
CREATE INDEX idx_blockers_workstream   ON blockers(workstream, status);

-- Cross-agent inbox (v1: one-to-one only; broadcast/multi-recipient is v2)
CREATE TABLE inbox (
    id            TEXT PRIMARY KEY,           -- ULID
    from_agent_id TEXT REFERENCES agents(id), -- NULL if from system / user
    from_label    TEXT,                       -- "system" | "user" | NULL when from_agent_id set
    to_agent_id   TEXT NOT NULL REFERENCES agents(id),
    subject       TEXT NOT NULL,
    body          TEXT NOT NULL,
    priority      TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low','normal','high','urgent')),
    sent_at       TEXT NOT NULL,
    read_at       TEXT,
    acked_at      TEXT,
    metadata_json TEXT
        CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);
CREATE INDEX idx_inbox_to_unacked     ON inbox(to_agent_id, acked_at, sent_at DESC);
CREATE INDEX idx_inbox_to_unread_prio ON inbox(to_agent_id, read_at, priority, sent_at DESC);
-- NOTE (phase-3 review): `priority` is stored as text, so a raw `ORDER BY
-- priority` sorts alphabetically (high < low < normal < urgent), not by
-- importance. The phase 4+ `todo_list` / inbox list tools MUST map priority to
-- an ordinal (CASE rank) when ordering — the text column is fine for equality
-- filters and counts, not for ordered listing.

-- Handovers replace the markdown files as the transport.
-- Two-phase claim / ack so Connects ingest can crash without losing data.
CREATE TABLE handovers (
    id            TEXT PRIMARY KEY,           -- ULID
    agent_id      TEXT NOT NULL REFERENCES agents(id),
    session_id    TEXT,
    saved_at      TEXT NOT NULL,
    summary       TEXT NOT NULL,              -- one-liner
    body_md       TEXT NOT NULL,              -- full structured body
    claimed_at    TEXT,                       -- set by handover_claim
    claimed_by    TEXT,                       -- consumer identifier
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    consumed_at   TEXT,                       -- set by handover_ack
    metadata_json TEXT
        CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);
CREATE INDEX idx_handovers_claimable ON handovers(consumed_at, claimed_at, saved_at);
CREATE INDEX idx_handovers_agent     ON handovers(agent_id, saved_at DESC);
CREATE INDEX idx_handovers_session   ON handovers(session_id);

-- Migration tracking
CREATE TABLE migrations (
    version    INTEGER PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

-- Backup throttle
CREATE TABLE backup_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    path        TEXT NOT NULL,
    success     INTEGER NOT NULL DEFAULT 0
);
```

**Conceptual model:**

- `notes` is an **append-mostly event stream** (notes are not edited in place;
  retraction is via `memory_delete`, soft by default — see
  [ADR 0005](decisions/0005-note-soft-delete.md)). Categories are minimal:
  `progress | decision | note`. Things like "I added a blocker" or "I closed a
  todo" generate a `note` row with `related_type='blocker'` / `related_id=…` —
  the stateful object lives in its own table.
- `todos`, `blockers`, `handovers` are **stateful objects**. They are the
  source of truth for state; notes reference them.
- This eliminates the overlap that originally had `category='blocker'` on
  notes *and* a `blockers` table, which would lead to double-counting in
  Connects ingest.

### MCP tool surface

Every tool scopes to the **calling agent**, resolved from the
**caller-supplied `agent_cwd: str` parameter** (canonicalized server-side
and looked up in `agent_aliases`). The server cannot infer the calling
agent's working directory from its own process state — see the Agent
identity model section. Cross-agent operations additionally require an
explicit `agent_id` (or `agent_name` / `agent_path`) parameter for the
target agent. Every list/query tool has a required (server-capped) `limit`
and supports cursor-based pagination. Large bodies (e.g., handover
`body_md`) are truncated with a `truncated: true` flag unless the caller
passes `full=true`.

In the table below, `agent_cwd` is implied on every row-marked tool except
`memory_backup_now` (server-scoped). If MCP `roots` capability turns out to
be supported by the Copilot CLI (decision deferred to Phase 3), the server
will accept `agent_cwd` as optional and fall back to `mcp.roots()`; the
parameter itself stays in the tool signature so callers that always pass it
keep working.

| Tool                  | Purpose                                                            |
|-----------------------|--------------------------------------------------------------------|
| `agent_register`      | Idempotent upsert of (name, workstream) + alias for calling path   |
| `agent_describe`      | Return calling agent's metadata + unread/todo/blocker counts       |
| `agent_add_alias`     | Add another path alias for the same agent (e.g., new work-tree)    |
| `agent_rekey`         | (Rare) merge two agents that got accidentally split                |
| `memory_log`          | Insert a note (`category`, `topic`, `content`, related, meta)      |
| `memory_query`        | List notes (filters: category, topic, since, limit, cursor)        |
| `memory_export`       | Render a window of notes as markdown for human reading             |
| `todo_add`            | Create a long-running todo                                         |
| `todo_list`           | List todos (status / priority filter, limit, cursor)               |
| `todo_update`         | Change status / due / priority / description                       |
| `blocker_open`        | Add a persistent blocker (auto-logs related note)                  |
| `blocker_list`        | List blockers                                                      |
| `blocker_resolve`     | Resolve a blocker (auto-logs related note)                         |
| `inbox_send`          | Send a message to another agent (by name or canonical path)        |
| `inbox_check`         | List unread (and optionally read) messages                         |
| `inbox_ack`           | Mark message read / acked                                          |
| `handover_save`       | Save a full handover (replaces markdown file writes)               |
| `handover_latest`     | Return the most recent handover for an agent                       |
| `handover_export`     | Render a handover as markdown                                      |
| `handover_claim`      | **Consumer-side:** set `claimed_at` + return rows. Stale claims    |
|                       | (> N minutes) are returnable to other consumers.                   |
| `handover_ack`        | **Consumer-side:** set `consumed_at` for claimed IDs               |
| `handover_release`    | **Consumer-side:** release a claim (with `last_error`)             |
| `memory_backup_now`   | Trigger an on-demand backup using the SQLite backup API            |

### Connects ingest (rewrite)

- The Connects `ingest-handovers` skill calls
  `handover_claim(consumer_id="connects-ingest")`, inserts the data into
  `data/progress.db` (with `source_system='np-agent-memory'`,
  `source_table='handovers'`, `source_id=<handover.id>`), then calls
  `handover_ack(ids)`.
- `data/progress.db` gets a `UNIQUE(source_system, source_table, source_id)`
  constraint on `daily_notes` (and analogues on `blockers` / `reminders`) so
  retries are idempotent.
- If ingest crashes between claim and ack, the claim expires and another run
  picks it up. No data is silently lost.
- The dashboard, link patterns, and existing tables in `progress.db` are
  unchanged.

### Skill changes

- **New skill (shipped in plugin):** `skills/agent-memory/SKILL.md` — explains
  the tools, conventions, "what to log vs. what to skip", good inbox-message
  patterns, and how to use `memory_export` when you want a markdown view.
  **MUST teach the `agent_cwd` contract**: every tool call passes the
  agent's repo root (`git rev-parse --show-toplevel` for git-backed agents,
  or a stable per-workstream root otherwise). The skill should include a
  copy-pasteable session-start snippet so each workstream agent registers
  itself idempotently on the first turn.
- **Rewrite:** `~/.copilot/skills/handover-report/SKILL.md` — becomes a thin
  skill that calls `handover_save` (no file writes). Structured-body template
  stays the same so the markdown shape is preserved.
- **Rewrite:** `.github/skills/ingest-handovers/SKILL.md` (in Connects) —
  point at the new MCP tools (claim/ack model).

## Implementation phases

Phases are roughly dependency-ordered; several can run in parallel.

0. **Spike: verify plugin packaging for stdio MCP servers.** Build a 1-tool
   hello-world plugin (`np-agent-memory-spike`), install it, confirm the
   Copilot CLI launches the stdio server, confirm working-dir / env / arg
   semantics. **No code beyond this phase until the spike works.**
1. **Plugin scaffolding** — create `NP.CoPilot.AgentMemory` repo with
   `.claude-plugin\`, `.mcp.json`, `README.md`, `install.ps1`. Empty MCP
   server loads cleanly.
2. **Data folder + migration runner** — create `$HOME\.copilot\np-agent-memory\`
   on first run, schema.sql, migrations folder, idempotent migrator with
   `BEGIN IMMEDIATE` + retry. Tests for two concurrent starts.
3. **MCP server skeleton** — WAL pragmas, per-call connections, busy retries,
   path canonicalization, `agent_register(agent_cwd=…)` + `agent_describe`
   end-to-end. **Before implementing**: spend ~30 minutes probing whether
   the Copilot CLI advertises the MCP `roots` capability (server logs
   `client.list_roots()` request, or inspect the initialize handshake). If
   supported, accept `agent_cwd` as optional with a `roots`-based fallback;
   if not, keep it required. Either way the wire shape doesn't change for
   callers that pass `agent_cwd` explicitly.
4. **Memory + todos tools** — `memory_log`, `memory_query`, `memory_export`,
   `todo_*`. Dogfood from the next session.
5. **Blockers + handovers tools** — `blocker_*`, `handover_save`,
   `handover_latest`, `handover_export`, and the **claim / ack pair**.
6. **Inbox tools** — `inbox_send / check / ack`.
7. **Backup machinery** — SQLite online-backup API, throttled, lazy.
8. **Bundled skill** — `skills/agent-memory/SKILL.md`.
9. **Connects ingest rewrite** — switch to `handover_claim` + `handover_ack`;
   add `source_*` columns + uniqueness constraints to `data/progress.db`.
10. **Rewrite global `handover-report` skill** to call `handover_save`.
11. **Agent instructions update** — install one global instruction at
    `~/.copilot/instructions/agent-memory-usage.instructions.md` (shipped as a
    versioned repo template) so every repo opts into agent-memory by default,
    instead of per-repo `copilot-instructions.md` edits.
12. **Optional backfill** — script to import existing handover markdown in
    `follow-ups\handovers\processed\` into `handovers` + derived `notes`.

## Open questions (need a decision before phase 0/1)

1. **MCP server language.** Python (matches `progress.py` tooling, mature
   `mcp` SDK, comfortable for Nick) vs Node/TypeScript (lighter install on
   Windows, closer to the Copilot CLI ecosystem). **Recommended: Python**,
   with a bundled venv via `install.ps1` so cold sessions don't fail on
   missing deps.
2. **Repo location.** `C:\path\to\NP.CoPilot.AgentMemory` next to
   `NP.CoPilot.Config`. **Recommended: yes.**
3. **Distribution.** Personal use only, or `marketplace.json` set up from day
   one so teammates can install with one command? **Recommended: shareable
   from day one** (cost is near zero; benefit is real).
4. **Inbox addressing.** By canonical path or by registered name (resolved
   via `agents.name`)? **Recommended: accept both** — names are nicer DX,
   paths are unambiguous.
5. **Backfill.** Import existing markdown handovers (in `processed\`) on
   day one? **Recommended: yes, behind a `--backfill` flag** so continuity is
   preserved.
6. **Dashboard.** Add an agent-memory page to the Connects Flask dashboard
   (open todos / unread inbox / recent activity per agent)? **Recommended: out
   of scope for v1**, but the data model supports it cleanly.
7. **Handover-doc cutover style.** Hard cutover (rewrite the skill at phase 10,
   markdown writes stop immediately) vs. **dual-write transition** (skill
   writes both file AND `handover_save` for 1–2 weeks, then markdown is turned
   off via a config flag). **Recommended: dual-write transition** — it's a
   one-line check in the skill and gives you a no-regret rollback window
   while the new pipe earns trust.

## Risks (and mitigations)

| Risk                                                       | Mitigation                                                        |
|------------------------------------------------------------|-------------------------------------------------------------------|
| Multiple MCP processes corrupt setup                       | Per-call connections, `BEGIN IMMEDIATE` migrations, no shared state |
| SQLite DB grows unbounded                                  | `memory_export` + periodic prune script in v1.x; FTS5 later        |
| Plugin upgrade wipes runtime DB                            | DB lives in `$HOME\.copilot\np-agent-memory\`, never inside plugin install path |
| Connects ingest crash between claim and ack loses data     | Two-phase ack; stale claims auto-release; idempotent ingest with `source_id` |
| Agent identity drift across paths (symlinks, case, moves)  | ULID `agent_id` is primary; path is alias; canonicalization on every call; `agent_add_alias` handles legit multi-path cases |
| Python interpreter missing or wrong version on launch      | `install.ps1` creates a venv, pins deps, registers absolute interpreter path in `.mcp.json` |
| Cross-workstream accidental damage from a confused prompt  | Default scoping to current agent; cross-agent ops require explicit identifier |
| Tool returns blow up context                               | Required `limit`, server-capped max, cursor pagination, truncated bodies |
| Killing markdown loses human audit trail                   | `memory_export` / `handover_export` regenerate markdown on demand; dual-write transition gives a rollback window |
| Runtime data folder confused with plugin install folder    | Plugin name (`np-agent-memory`) used for BOTH the install dir under `installed-plugins\_direct\` AND the runtime dir under `$HOME\.copilot\` so the relationship is obvious |

## Out of scope (for v1)

- Encryption at rest (single-user machine, DB in `$HOME`)
- Multi-machine sync
- FTS5 search (use SQL filters until volume demands it)
- Multi-recipient or broadcast-to-workstream inbox
- A dedicated agent-memory dashboard page (Connects dashboard stays as-is)
- Token-budget-aware retrieval (e.g., "give me the 5 most relevant notes")
