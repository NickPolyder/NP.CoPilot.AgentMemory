"""Memory tools: append, query, and export agent notes.

Notes are the agent's durable, cross-session memory. ``memory_log`` appends;
``memory_query`` reads back a keyset-paginated, optionally-truncated window;
``memory_export`` renders a window as human-readable markdown. All three are
scoped to the calling agent via ``agent_cwd`` (see ``tools/agents.py`` for the
identity/trust model). Note ``content`` is agent-controlled and untrusted.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any, Literal, get_args

from mcp.server.fastmcp import FastMCP
from pydantic import Field, StrictBool

from np_agent_memory.db import open_connection, run_in_read_txn, run_in_write_txn
from np_agent_memory.identity import canonicalize_agent_cwd, new_ulid, now_iso
from np_agent_memory.tools._common import (
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    metadata_to_json,
    require_agent_id,
    truncate,
    validate_id_batch,
)

# Note categories — single source of truth for both the JSON-schema enum (via
# the Literal on the tool signature) and runtime validation (the derived tuple).
# Mirrors the CHECK constraint on notes.category.
_CategoryLiteral = Literal["progress", "decision", "note"]
_CATEGORIES = get_args(_CategoryLiteral)

# Length caps on agent-supplied note fields. Generous vs real use; guard the DB
# and tool responses against a pathological caller.
_MAX_TOPIC_LEN = 256
_MAX_CONTENT_LEN = 65_536
_MAX_RELATED_LEN = 256
_MAX_SESSION_LEN = 256

# memory_query clips content to this many chars unless full=True.
_CONTENT_PREVIEW_LEN = 2_000

# Upper bound on how many ids a single delete/restore call may target. Keeps the
# IN (...) parameter list well under SQLite's variable limit and turns a
# pathological request into a friendly error instead of an opaque DB failure.
_MAX_NOTE_IDS = 200

_NOTE_COLUMNS = (
    "id, timestamp, category, topic, content, session_id, "
    "related_type, related_id, metadata_json, deleted_at"
)


def _row_to_note(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    """Shape a notes row for a tool response, truncating content unless full."""
    content = row["content"]
    truncated = False
    if not full:
        content, truncated = truncate(content, _CONTENT_PREVIEW_LEN)
    note: dict[str, Any] = {
        "id": row["id"],
        "timestamp": row["timestamp"],
        "category": row["category"],
        "topic": row["topic"],
        "content": content,
        "session_id": row["session_id"],
        "related_type": row["related_type"],
        "related_id": row["related_id"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
        "deleted_at": row["deleted_at"],
    }
    if truncated:
        note["content_truncated"] = True
        note["content_length"] = len(row["content"])
    return note


def append_note(
    c: sqlite3.Connection,
    *,
    agent_id: str,
    category: str,
    content: str,
    topic: str | None = None,
    related_type: str | None = None,
    related_id: str | None = None,
    session_id: str | None = None,
    metadata_json: str | None = None,
) -> tuple[str, str]:
    """Insert a note row within the caller's transaction; return ``(id, ts)``.

    The single writer for the ``notes`` table. Other domains that auto-log a
    timeline event (e.g. blockers) call this instead of hand-rolling the notes
    schema/category knowledge, so all note writes stay in one place.
    """
    note_id = new_ulid()
    ts = now_iso()
    c.execute(
        "INSERT INTO notes "
        "(id, agent_id, timestamp, category, topic, content, session_id, "
        "related_type, related_id, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            note_id,
            agent_id,
            ts,
            category,
            topic,
            content,
            session_id,
            related_type,
            related_id,
            metadata_json,
        ),
    )
    return note_id, ts


def log_memory(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    category: str,
    content: str,
    topic: str | None = None,
    related_type: str | None = None,
    related_id: str | None = None,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a note for the calling agent and return its id + timestamp."""
    if category not in _CATEGORIES:
        raise ValueError(f"category must be one of {_CATEGORIES}, got {category!r}.")
    if not content or not content.strip():
        raise ValueError("content must be a non-empty, non-whitespace string.")
    if len(content) > _MAX_CONTENT_LEN:
        raise ValueError(f"content is too long (max {_MAX_CONTENT_LEN} chars).")
    if topic is not None and len(topic) > _MAX_TOPIC_LEN:
        raise ValueError(f"topic is too long (max {_MAX_TOPIC_LEN} chars).")
    if related_type is not None and len(related_type) > _MAX_RELATED_LEN:
        raise ValueError(f"related_type is too long (max {_MAX_RELATED_LEN} chars).")
    if related_id is not None and len(related_id) > _MAX_RELATED_LEN:
        raise ValueError(f"related_id is too long (max {_MAX_RELATED_LEN} chars).")
    if session_id is not None and len(session_id) > _MAX_SESSION_LEN:
        raise ValueError(f"session_id is too long (max {_MAX_SESSION_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)
    metadata_json = metadata_to_json(metadata)

    def _work(c: sqlite3.Connection) -> tuple[str, str]:
        agent_id = require_agent_id(c, canonical)
        return append_note(
            c,
            agent_id=agent_id,
            category=category,
            content=content,
            topic=topic,
            related_type=related_type,
            related_id=related_id,
            session_id=session_id,
            metadata_json=metadata_json,
        )

    note_id, ts = run_in_write_txn(conn, _work)
    return {"id": note_id, "timestamp": ts, "category": category, "topic": topic}


def _fetch_notes(
    conn: sqlite3.Connection,
    *,
    canonical: str,
    limit: int,
    category: str | None,
    topic: str | None,
    since: str | None,
    cursor: str | None,
    include_deleted: bool = False,
) -> tuple[list[sqlite3.Row], bool]:
    """Shared keyset read for query/export. Returns (rows, has_more).

    Soft-deleted notes (``deleted_at`` set) are hidden unless ``include_deleted``.
    """
    if category is not None and category not in _CATEGORIES:
        raise ValueError(f"category must be one of {_CATEGORIES}, got {category!r}.")

    cursor_key = decode_cursor(cursor) if cursor else None
    if cursor_key is not None and len(cursor_key) != 2:
        raise ValueError("invalid cursor for memory query.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        agent_id = require_agent_id(c, canonical)
        clauses = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if topic is not None:
            clauses.append("topic = ?")
            params.append(topic)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if cursor_key is not None:
            frag, frag_params = keyset_predicate(
                [("timestamp", cursor_key[0]), ("id", cursor_key[1])], direction="<"
            )
            clauses.append(frag)
            params.extend(frag_params)
        params.append(limit + 1)
        return c.execute(
            f"SELECT {_NOTE_COLUMNS} FROM notes WHERE {' AND '.join(clauses)} "
            f"ORDER BY timestamp DESC, id DESC LIMIT ?",
            params,
        ).fetchall()

    rows = run_in_read_txn(conn, _work)
    has_more = len(rows) > limit
    return rows[:limit], has_more


def query_memory(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    limit: int,
    category: str | None = None,
    topic: str | None = None,
    since: str | None = None,
    cursor: str | None = None,
    full: bool = False,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """Return a keyset-paginated window of the agent's notes, newest first."""
    limit = clamp_limit(limit)
    canonical = canonicalize_agent_cwd(agent_cwd)
    rows, has_more = _fetch_notes(
        conn,
        canonical=canonical,
        limit=limit,
        category=category,
        topic=topic,
        since=since,
        cursor=cursor,
        include_deleted=include_deleted,
    )
    notes = [_row_to_note(r, full=full) for r in rows]
    next_cursor = (
        encode_cursor([rows[-1]["timestamp"], rows[-1]["id"]])
        if has_more and rows
        else None
    )
    return {"notes": notes, "count": len(notes), "next_cursor": next_cursor}


def export_memory(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    limit: int,
    category: str | None = None,
    topic: str | None = None,
    since: str | None = None,
    cursor: str | None = None,
    include_deleted: bool = False,
) -> dict[str, Any]:
    """Render a window of the agent's notes as markdown (full content).

    Soft-deleted notes (``deleted_at`` set) are hidden unless ``include_deleted``.
    """
    limit = clamp_limit(limit)
    canonical = canonicalize_agent_cwd(agent_cwd)
    rows, has_more = _fetch_notes(
        conn,
        canonical=canonical,
        limit=limit,
        category=category,
        topic=topic,
        since=since,
        cursor=cursor,
        include_deleted=include_deleted,
    )

    lines: list[str] = ["# Agent memory", ""]
    if not rows:
        lines.append("_No notes._")
    current_day: str | None = None
    for row in rows:
        day = row["timestamp"][:10]
        if day != current_day:
            lines.extend([f"## {day}", ""])
            current_day = day
        topic_part = f" — {row['topic']}" if row["topic"] else ""
        deleted_part = " _(deleted)_" if row["deleted_at"] else ""
        lines.append(f"### `{row['category']}`{topic_part}{deleted_part}")
        lines.append(f"_{row['timestamp']}_")
        lines.append("")
        lines.append(row["content"])
        lines.append("")

    next_cursor = (
        encode_cursor([rows[-1]["timestamp"], rows[-1]["id"]])
        if has_more and rows
        else None
    )
    return {
        "markdown": "\n".join(lines),
        "count": len(rows),
        "next_cursor": next_cursor,
    }


def _validate_note_ids(ids: list[str]) -> list[str]:
    """Validate and de-duplicate a caller-supplied list of note ids."""
    return validate_id_batch(ids, label="ids", max_count=_MAX_NOTE_IDS)


def delete_notes(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    ids: list[str],
    hard: bool = False,
) -> dict[str, Any]:
    """Delete notes belonging to the calling agent.

    ``hard=False`` (the default) is a reversible *soft* delete: it stamps
    ``deleted_at`` so the note stops appearing in ``memory_query`` /
    ``memory_export`` but the row is preserved and can be restored with
    ``restore_notes``. ``hard=True`` permanently removes the row and is
    irreversible — callers must confirm with the user first (see the tool
    docstring / SKILL).

    Only the calling agent's own notes are touched; ids that belong to another
    agent (or do not exist) are returned in ``not_found`` — this deliberately
    does not distinguish "not yours" from "does not exist" so it cannot confirm
    another agent's note ids. Soft-deleting an already soft-deleted note is a
    no-op reported in ``skipped``.
    """
    if not isinstance(hard, bool):
        raise ValueError("hard must be a boolean.")
    ids = _validate_note_ids(ids)

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> dict[str, Any]:
        agent_id = require_agent_id(c, canonical)
        marks = ",".join("?" for _ in ids)
        owned = c.execute(
            f"SELECT id, deleted_at FROM notes WHERE agent_id = ? AND id IN ({marks})",
            (agent_id, *ids),
        ).fetchall()
        owned_ids = {row["id"] for row in owned}
        not_found = [note_id for note_id in ids if note_id not in owned_ids]

        if hard is True:
            c.execute(
                f"DELETE FROM notes WHERE agent_id = ? AND id IN ({marks})",
                (agent_id, *ids),
            )
            deleted_ids = [row["id"] for row in owned]
            return {
                "mode": "hard",
                "deleted": len(deleted_ids),
                "deleted_ids": deleted_ids,
                "skipped": [],
                "not_found": not_found,
            }

        already = [row["id"] for row in owned if row["deleted_at"] is not None]
        to_delete = [row["id"] for row in owned if row["deleted_at"] is None]
        if to_delete:
            del_marks = ",".join("?" for _ in to_delete)
            c.execute(
                f"UPDATE notes SET deleted_at = ? "
                f"WHERE agent_id = ? AND id IN ({del_marks})",
                (now_iso(), agent_id, *to_delete),
            )
        return {
            "mode": "soft",
            "deleted": len(to_delete),
            "deleted_ids": to_delete,
            "skipped": already,
            "not_found": not_found,
        }

    return run_in_write_txn(conn, _work)


def restore_notes(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    ids: list[str],
) -> dict[str, Any]:
    """Restore soft-deleted notes belonging to the calling agent.

    Clears ``deleted_at`` so the note reappears in ``memory_query`` /
    ``memory_export``. This is the inverse of a soft ``delete_notes`` — it cannot
    recover a hard-deleted (permanently removed) note. Restoring a note that is
    already live is a no-op reported in ``skipped``; ids that are not the
    caller's notes are returned in ``not_found``.
    """
    ids = _validate_note_ids(ids)

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> dict[str, Any]:
        agent_id = require_agent_id(c, canonical)
        marks = ",".join("?" for _ in ids)
        owned = c.execute(
            f"SELECT id, deleted_at FROM notes WHERE agent_id = ? AND id IN ({marks})",
            (agent_id, *ids),
        ).fetchall()
        owned_ids = {row["id"] for row in owned}
        not_found = [note_id for note_id in ids if note_id not in owned_ids]

        already_live = [row["id"] for row in owned if row["deleted_at"] is None]
        to_restore = [row["id"] for row in owned if row["deleted_at"] is not None]
        if to_restore:
            res_marks = ",".join("?" for _ in to_restore)
            c.execute(
                f"UPDATE notes SET deleted_at = NULL "
                f"WHERE agent_id = ? AND id IN ({res_marks})",
                (agent_id, *to_restore),
            )
        return {
            "restored": len(to_restore),
            "restored_ids": to_restore,
            "skipped": already_live,
            "not_found": not_found,
        }

    return run_in_write_txn(conn, _work)


def register_memory_tools(mcp: FastMCP) -> None:
    """Register the memory tools on the FastMCP server."""

    @mcp.tool()
    def memory_log(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        category: Annotated[
            _CategoryLiteral,
            Field(
                description=(
                    'Note kind: "progress" (a meaningful step forward), '
                    '"decision" (a choice and its rationale), or "note" '
                    "(durable context/gotcha)."
                )
            ),
        ],
        content: Annotated[
            str,
            Field(
                description=(
                    "The note body text — this is the main field. Do NOT pass it "
                    "as 'summary'; memory_log has no summary field. Non-empty."
                )
            ),
        ],
        topic: Annotated[
            str | None,
            Field(description="Optional short subject for filtering/grouping."),
        ] = None,
        related_type: Annotated[
            str | None,
            Field(description='Optional kind of related entity (e.g. "todo", "pr").'),
        ] = None,
        related_id: Annotated[
            str | None,
            Field(description="Optional id of the related entity."),
        ] = None,
        session_id: Annotated[
            str | None,
            Field(description="Optional id of the session that produced this note."),
        ] = None,
        metadata: Annotated[
            dict[str, Any] | None,
            Field(
                description="Optional JSON object (not a string) of structured extras."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Append a note to your durable, cross-session memory.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            category: One of "progress", "decision", or "note".
            content: The note body (non-empty).
            topic: Optional short subject for filtering/grouping.
            related_type: Optional kind of a related entity (e.g. "todo", "pr").
            related_id: Optional id of the related entity.
            session_id: Optional id of the session that produced this note.
            metadata: Optional JSON object of structured extras.

        Returns:
            The new note's ``id``, ``timestamp``, ``category`` and ``topic``.
        """
        with open_connection() as conn:
            return log_memory(
                conn,
                agent_cwd=agent_cwd,
                category=category,
                content=content,
                topic=topic,
                related_type=related_type,
                related_id=related_id,
                session_id=session_id,
                metadata=metadata,
            )

    @mcp.tool()
    def memory_query(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        limit: Annotated[
            int, Field(description="Max notes to return (server-capped at 200).")
        ],
        category: Annotated[
            _CategoryLiteral | None,
            Field(description='Optional filter — "progress", "decision", or "note".'),
        ] = None,
        topic: Annotated[
            str | None, Field(description="Optional exact-topic filter.")
        ] = None,
        since: Annotated[
            str | None,
            Field(
                description="Optional ISO-8601 lower bound on timestamp (inclusive)."
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque token from a previous call's next_cursor."),
        ] = None,
        full: Annotated[
            bool, Field(description="Return untruncated content when true.")
        ] = False,
        include_deleted: Annotated[
            bool,
            Field(description="Include soft-deleted notes (deleted_at set) when true."),
        ] = False,
    ) -> dict[str, Any]:
        """List your notes, newest first, with keyset pagination.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max notes to return (server-capped).
            category: Optional filter — "progress", "decision", or "note".
            topic: Optional exact-topic filter.
            since: Optional ISO-8601 lower bound on ``timestamp`` (inclusive).
            cursor: Opaque token from a previous call's ``next_cursor``.
            full: Return untruncated ``content`` when true.
            include_deleted: Include soft-deleted notes when true (default
                hides them).

        Returns:
            ``notes`` (each with content truncated unless ``full``, and a
            ``deleted_at`` field that is null for live notes), ``count``,
            and ``next_cursor`` (null when there is no further page).
        """
        with open_connection() as conn:
            return query_memory(
                conn,
                agent_cwd=agent_cwd,
                limit=limit,
                category=category,
                topic=topic,
                since=since,
                cursor=cursor,
                full=full,
                include_deleted=include_deleted,
            )

    @mcp.tool()
    def memory_export(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        limit: Annotated[
            int, Field(description="Max notes to render (server-capped at 200).")
        ],
        category: Annotated[
            _CategoryLiteral | None,
            Field(description='Optional filter — "progress", "decision", or "note".'),
        ] = None,
        topic: Annotated[
            str | None, Field(description="Optional exact-topic filter.")
        ] = None,
        since: Annotated[
            str | None,
            Field(
                description="Optional ISO-8601 lower bound on timestamp (inclusive)."
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque token from a previous call's next_cursor."),
        ] = None,
        include_deleted: Annotated[
            bool,
            Field(
                description=(
                    "Include soft-deleted notes (marked _(deleted)_) when true; "
                    "they are hidden by default."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Render a window of your notes as human-readable markdown.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max notes to render (server-capped).
            category: Optional filter — "progress", "decision", or "note".
            topic: Optional exact-topic filter.
            since: Optional ISO-8601 lower bound on ``timestamp`` (inclusive).
            cursor: Opaque token from a previous call's ``next_cursor``.
            include_deleted: Include soft-deleted notes when true (default
                hides them); included ones are marked _(deleted)_.

        Returns:
            ``markdown`` (full content, grouped by day → category), ``count``,
            and ``next_cursor`` (null when there is no further page) which you
            pass back as ``cursor`` to render the next page.
        """
        with open_connection() as conn:
            return export_memory(
                conn,
                agent_cwd=agent_cwd,
                limit=limit,
                category=category,
                topic=topic,
                since=since,
                cursor=cursor,
                include_deleted=include_deleted,
            )

    @mcp.tool()
    def memory_delete(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        ids: Annotated[
            list[str],
            Field(
                description="Non-empty list of note ids (from memory_query) to delete."
            ),
        ],
        hard: Annotated[
            StrictBool,
            Field(
                description=(
                    "false (default) = reversible SOFT delete (hides the note "
                    "but keeps the row; restore with memory_restore). true = "
                    "PERMANENT, IRREVERSIBLE hard delete — only pass true after "
                    "the user has explicitly confirmed they want the notes "
                    "destroyed."
                )
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Delete your own notes (soft by default).

        Soft delete (``hard=false``) stamps ``deleted_at`` so the note no longer
        shows in ``memory_query`` / ``memory_export`` (pass ``include_deleted``
        to see it); the row is preserved and can be brought back with
        ``memory_restore`` or hard-deleted later. Hard delete (``hard=true``)
        permanently removes the row and cannot be undone — **always confirm with
        the user before calling with ``hard=true``.** Only your own notes are
        affected.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            ids: Note ids to delete (non-empty list).
            hard: Permanently remove instead of soft-deleting. Requires explicit
                user confirmation.

        Returns:
            ``mode`` ("soft"/"hard"), ``deleted`` count, ``deleted_ids``,
            ``skipped`` (already soft-deleted) and ``not_found`` (ids that are
            not your notes).
        """
        with open_connection() as conn:
            return delete_notes(conn, agent_cwd=agent_cwd, ids=ids, hard=hard)

    @mcp.tool()
    def memory_restore(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        ids: Annotated[
            list[str],
            Field(
                description=(
                    "Non-empty list of note ids to restore (find soft-deleted "
                    "ids via memory_query with include_deleted=true)."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Restore your own soft-deleted notes (undo a soft delete).

        Clears ``deleted_at`` so the note reappears in ``memory_query`` /
        ``memory_export``. This is the inverse of a soft ``memory_delete`` — it
        cannot recover a note that was hard-deleted. Restoring a note that is
        already live is a no-op (reported in ``skipped``). Only your own notes
        are affected.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            ids: Note ids to restore (non-empty list).

        Returns:
            ``restored`` count, ``restored_ids``, ``skipped`` (already live) and
            ``not_found`` (ids that are not your notes).
        """
        with open_connection() as conn:
            return restore_notes(conn, agent_cwd=agent_cwd, ids=ids)
