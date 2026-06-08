# Phase 4 — Memory + Todos tools

Status: **done ✅**. Implements the six agent-scoped persistence tools on
top of the `notes` and `todos` tables already created by `0001_init.sql`
(no new migration required). 53 new tests; full suite 174 passing, ruff clean.

## Scope

| Tool | Table | Kind | Summary |
|---|---|---|---|
| `memory_log` | `notes` | write | Append a note (`category`, `topic`, `content`, related ref, metadata). |
| `memory_query` | `notes` | read | Keyset-paginated list (filters: `category`, `topic`, `since`). Truncates `content` unless `full=true`. |
| `memory_export` | `notes` | read | Render a window of notes as human-readable markdown (full content, grouped by date → category). |
| `todo_add` | `todos` | write | Create a long-running todo (`title`, `description`, `priority`, `due_date`). |
| `todo_list` | `todos` | read | Keyset-paginated list (filters: `status`, `priority`; `sort` = `recent`\|`priority`). |
| `todo_update` | `todos` | write | Change `status` / `priority` / `due_date` / `description`; manages `completed_at`. |

Out of scope (later phases): blockers, handovers, inbox (5/6), backups (7),
FTS5 search, prune/retention.

## Conventions (apply to every tool here)

- **Module layout** — `tools/memory.py` + `tools/todos.py`, each exposing
  `register_<domain>_tools(mcp)`, wired via `tools/__init__.register_all_tools`.
  Pure DB-logic functions take a `sqlite3.Connection`; thin `@mcp.tool`
  wrappers open a per-call connection. Matches `tools/agents.py`.
- **Identity** — every tool takes explicit `agent_cwd`, canonicalized via
  `canonicalize_agent_cwd`, resolved to an internal `agent_id` through
  `agent_aliases`. Agents never see the internal ULID. An unregistered (but
  valid) `agent_cwd` raises `ValueError("… Call agent_register first.")` — we do
  **not** silently return empty results, which would mask a typo.
- **Transactions** — writes via `run_in_write_txn` (BEGIN IMMEDIATE + retry),
  reads via `run_in_read_txn` (single consistent snapshot).
- **Resource IDs are visible** — note/todo ULIDs are returned and accepted
  (e.g. `todo_update(todo_id=…)`). The "never expose internal IDs" rule is about
  *agent identity*, not resource handles the caller must reference.
- **Required, server-capped `limit`** — list/query tools take a required
  `limit: int`, clamped to `[1, _MAX_LIMIT]` server-side.
- **Keyset (cursor) pagination** — no OFFSET scans. The cursor is an opaque
  base64 token over the ordering key. List tools fetch `limit + 1` rows to
  detect a further page and return `next_cursor` (or `null`).
- **Body truncation** — `memory_query` truncates `content` to
  `_CONTENT_PREVIEW_LEN` and sets `content_truncated: true` unless `full=true`.
  `memory_export` always renders full content.
- **Validation at the boundary** — categories/statuses/priorities validated
  against the same enums as the SQL `CHECK` constraints, with clean messages;
  length caps on `topic`/`content`/`title`/`description`; `metadata` accepted as
  an object and stored as JSON (`json_valid` enforced by the schema).

## Ordering decisions

- **`memory_query` / `memory_export`**: newest first — `ORDER BY timestamp DESC,
  id DESC`, backed by `idx_notes_agent_time`. Cursor key = `(timestamp, id)`.
  ISO-8601 UTC timestamps sort lexicographically, so string comparison is
  correct.
- **`todo_list`**:
  - `sort="recent"` (default): `ORDER BY created_at DESC, id DESC`. Cursor key =
    `(created_at, id)`.
  - `sort="priority"`: `ORDER BY <priority_rank> DESC, created_at DESC, id DESC`,
    where `priority_rank` is an explicit ordinal CASE
    (`low=0, normal=1, high=2, urgent=3`) — **not** the raw text column.
    This discharges the phase-3 review caveat (PLAN.md lines 296–300): text
    priority must be mapped to an ordinal for ordered listing. Cursor key =
    `(priority_rank, created_at, id)`.
- A generic `keyset_predicate(keys, direction)` helper builds the
  `(a<?) OR (a=? AND b<?) OR …` WHERE fragment from an ordered list of
  `(sql_expr, value)` pairs, so both sorts share one correct implementation.

## Shared helpers — `tools/_common.py`

- `resolve_agent_id(c, canonical) -> str | None` — alias lookup (also
  back-fills the three inlined lookups in `agents.py`).
- `require_agent_id(c, canonical) -> str` — raises if unregistered.
- `clamp_limit(limit) -> int` — validate + cap.
- `encode_cursor(values) -> str` / `decode_cursor(token) -> list` — opaque
  base64-JSON; `decode` raises `ValueError` on a malformed token.
- `keyset_predicate(keys, direction="<") -> (sql, params)`.
- `truncate(text, length) -> (preview, was_truncated)`.

## Verification

- Unit tests `tests/test_common.py`, `tests/test_memory.py`,
  `tests/test_todos.py` in the `test_agents.py` style (fresh temp DB, register a
  test agent, exercise happy path + validation + pagination + truncation +
  cross-agent isolation + `completed_at` transitions).
- `ruff check` / `ruff format --check`, full `pytest` green.

## Follow-ups / notes

- Dogfood from the next session once installed (the plugin now installs cleanly
  — see the marketplace/`autoUpdate` fix in session history).
- `metadata` round-trips as a JSON object; deep validation of its shape is the
  caller's concern (stored as untrusted, per the agents.py trust note).
