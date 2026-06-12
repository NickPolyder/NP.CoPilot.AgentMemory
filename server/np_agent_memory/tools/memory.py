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
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory.db import open_connection, run_in_read_txn, run_in_write_txn
from np_agent_memory.identity import canonicalize_agent_cwd, new_ulid, now_iso
from np_agent_memory.tools._common import (
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    require_agent_id,
    truncate,
)

# Note categories — mirrors the CHECK constraint on notes.category.
_CATEGORIES = ("progress", "decision", "note")

# Length caps on agent-supplied note fields. Generous vs real use; guard the DB
# and tool responses against a pathological caller.
_MAX_TOPIC_LEN = 256
_MAX_CONTENT_LEN = 65_536
_MAX_RELATED_LEN = 256
_MAX_SESSION_LEN = 256

# memory_query clips content to this many chars unless full=True.
_CONTENT_PREVIEW_LEN = 2_000

_NOTE_COLUMNS = (
    "id, timestamp, category, topic, content, session_id, "
    "related_type, related_id, metadata_json"
)


def _metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    """Serialize a metadata object to a JSON string, or ``None``."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object (mapping), not a scalar/array.")
    return json.dumps(metadata, separators=(",", ":"))


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
    }
    if truncated:
        note["content_truncated"] = True
        note["content_length"] = len(row["content"])
    return note


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
    metadata_json = _metadata_to_json(metadata)

    def _work(c: sqlite3.Connection) -> tuple[str, str]:
        agent_id = require_agent_id(c, canonical)
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
) -> tuple[list[sqlite3.Row], bool]:
    """Shared keyset read for query/export. Returns (rows, has_more)."""
    if category is not None and category not in _CATEGORIES:
        raise ValueError(f"category must be one of {_CATEGORIES}, got {category!r}.")

    cursor_key = decode_cursor(cursor) if cursor else None
    if cursor_key is not None and len(cursor_key) != 2:
        raise ValueError("invalid cursor for memory query.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        agent_id = require_agent_id(c, canonical)
        clauses = ["agent_id = ?"]
        params: list[Any] = [agent_id]
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
) -> dict[str, Any]:
    """Render a window of the agent's notes as markdown (full content)."""
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
        lines.append(f"### `{row['category']}`{topic_part}")
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


def register_memory_tools(mcp: FastMCP) -> None:
    """Register the memory tools on the FastMCP server."""

    @mcp.tool()
    def memory_log(
        agent_cwd: str,
        category: str,
        content: str,
        topic: str | None = None,
        related_type: str | None = None,
        related_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
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
        agent_cwd: str,
        limit: int,
        category: str | None = None,
        topic: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
        full: bool = False,
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

        Returns:
            ``notes`` (each with content truncated unless ``full``), ``count``,
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
            )

    @mcp.tool()
    def memory_export(
        agent_cwd: str,
        limit: int,
        category: str | None = None,
        topic: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Render a window of your notes as human-readable markdown.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max notes to render (server-capped).
            category: Optional filter — "progress", "decision", or "note".
            topic: Optional exact-topic filter.
            since: Optional ISO-8601 lower bound on ``timestamp`` (inclusive).
            cursor: Opaque token from a previous call's ``next_cursor``.

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
            )
