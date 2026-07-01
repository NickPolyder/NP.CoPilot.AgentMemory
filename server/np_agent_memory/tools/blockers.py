"""Blocker tools: open, list, and resolve persistent blockers.

Blockers are durable, cross-session impediments scoped to the calling agent via
``agent_cwd`` (see ``tools/agents.py`` for the identity/trust model). Opening
and resolving a blocker also append a related note to the agent's memory so the
event shows up in the timeline. ``external_key`` is an optional caller-supplied
idempotency key, unique per agent.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any, Literal, get_args

from mcp.server.fastmcp import FastMCP
from pydantic import Field

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
from np_agent_memory.tools.memory import append_note

# Mirrors the CHECK constraint on blockers.status. The Literal feeds the
# JSON-schema enum; the derived tuple feeds runtime validation.
_StatusLiteral = Literal["active", "escalated", "resolved"]
_STATUSES = get_args(_StatusLiteral)

_MAX_TITLE_LEN = 256
_MAX_DESCRIPTION_LEN = 8_192
_MAX_KEY_LEN = 128
_MAX_OWNER_LEN = 256
_MAX_WORKSTREAM_LEN = 256
_MAX_RESOLUTION_LEN = 8_192

# blocker_list clips description to this many chars unless full=True.
_DESCRIPTION_PREVIEW_LEN = 500

_BLOCKER_COLUMNS = (
    "id, external_key, title, description, owner, workstream, status, "
    "raised_at, escalated_at, resolved_at, resolution"
)


def _insert_blocker_note(
    c: sqlite3.Connection, agent_id: str, blocker_id: str, content: str
) -> None:
    """Append a note about a blocker event, in the same transaction.

    Routes through the notes aggregate's writer (``memory.append_note``) so the
    ``notes`` schema/category knowledge lives in one module.
    """
    append_note(
        c,
        agent_id=agent_id,
        category="note",
        topic="blocker",
        content=content,
        related_type="blocker",
        related_id=blocker_id,
    )


def _row_to_blocker(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    """Shape a blockers row for a tool response, truncating description."""
    description = row["description"]
    truncated = False
    if not full and description is not None:
        description, truncated = truncate(description, _DESCRIPTION_PREVIEW_LEN)
    blocker: dict[str, Any] = {
        "id": row["id"],
        "external_key": row["external_key"],
        "title": row["title"],
        "description": description,
        "owner": row["owner"],
        "workstream": row["workstream"],
        "status": row["status"],
        "raised_at": row["raised_at"],
        "escalated_at": row["escalated_at"],
        "resolved_at": row["resolved_at"],
        "resolution": row["resolution"],
    }
    if truncated:
        blocker["description_truncated"] = True
        blocker["description_length"] = len(row["description"])
    return blocker


def open_blocker(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    title: str,
    description: str | None = None,
    owner: str | None = None,
    workstream: str | None = None,
    external_key: str | None = None,
) -> dict[str, Any]:
    """Open a persistent blocker for the calling agent and auto-log a note."""
    if not title or not title.strip():
        raise ValueError("title must be a non-empty, non-whitespace string.")
    if len(title) > _MAX_TITLE_LEN:
        raise ValueError(f"title is too long (max {_MAX_TITLE_LEN} chars).")
    if description is not None and len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(f"description is too long (max {_MAX_DESCRIPTION_LEN} chars).")
    if external_key is not None and len(external_key) > _MAX_KEY_LEN:
        raise ValueError(f"external_key is too long (max {_MAX_KEY_LEN} chars).")
    if owner is not None and len(owner) > _MAX_OWNER_LEN:
        raise ValueError(f"owner is too long (max {_MAX_OWNER_LEN} chars).")
    if workstream is not None and len(workstream) > _MAX_WORKSTREAM_LEN:
        raise ValueError(f"workstream is too long (max {_MAX_WORKSTREAM_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> tuple[str, str]:
        agent_id = require_agent_id(c, canonical)
        if external_key is not None:
            existing = c.execute(
                "SELECT id FROM blockers WHERE agent_id = ? AND external_key = ?",
                (agent_id, external_key),
            ).fetchone()
            if existing is not None:
                raise ValueError(
                    f"a blocker with external_key {external_key!r} already exists "
                    f"for this agent (id {existing['id']})."
                )
        blocker_id = new_ulid()
        ts = now_iso()
        c.execute(
            "INSERT INTO blockers "
            "(id, agent_id, external_key, title, description, owner, workstream, "
            "status, raised_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (
                blocker_id,
                agent_id,
                external_key,
                title,
                description,
                owner,
                workstream,
                ts,
            ),
        )
        _insert_blocker_note(c, agent_id, blocker_id, f"Blocker opened: {title}")
        return blocker_id, ts

    blocker_id, ts = run_in_write_txn(conn, _work)
    return {
        "id": blocker_id,
        "title": title,
        "status": "active",
        "raised_at": ts,
        "external_key": external_key,
    }


def list_blockers(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    limit: int,
    status: str | None = None,
    workstream: str | None = None,
    cursor: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Return a keyset-paginated window of the agent's blockers, newest first."""
    limit = clamp_limit(limit)
    if status is not None and status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, got {status!r}.")

    canonical = canonicalize_agent_cwd(agent_cwd)
    cursor_key = decode_cursor(cursor) if cursor else None
    if cursor_key is not None and len(cursor_key) != 2:
        raise ValueError("invalid cursor for blocker list.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        agent_id = require_agent_id(c, canonical)
        clauses = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if workstream is not None:
            clauses.append("workstream = ?")
            params.append(workstream)
        if cursor_key is not None:
            frag, frag_params = keyset_predicate(
                [("raised_at", cursor_key[0]), ("id", cursor_key[1])], direction="<"
            )
            clauses.append(frag)
            params.extend(frag_params)
        params.append(limit + 1)
        return c.execute(
            f"SELECT {_BLOCKER_COLUMNS} FROM blockers WHERE {' AND '.join(clauses)} "
            f"ORDER BY raised_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()

    rows = run_in_read_txn(conn, _work)
    has_more = len(rows) > limit
    rows = rows[:limit]
    blockers = [_row_to_blocker(r, full=full) for r in rows]
    next_cursor = (
        encode_cursor([rows[-1]["raised_at"], rows[-1]["id"]])
        if has_more and rows
        else None
    )
    return {"blockers": blockers, "count": len(blockers), "next_cursor": next_cursor}


def resolve_blocker(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    blocker_id: str,
    resolution: str | None = None,
) -> dict[str, Any]:
    """Resolve one of the agent's blockers and auto-log a note."""
    if resolution is not None and len(resolution) > _MAX_RESOLUTION_LEN:
        raise ValueError(f"resolution is too long (max {_MAX_RESOLUTION_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> sqlite3.Row:
        agent_id = require_agent_id(c, canonical)
        existing = c.execute(
            "SELECT id, title, status FROM blockers WHERE id = ? AND agent_id = ?",
            (blocker_id, agent_id),
        ).fetchone()
        if existing is None:
            raise ValueError(f"blocker {blocker_id!r} not found for this agent.")
        if existing["status"] == "resolved":
            raise ValueError(f"blocker {blocker_id!r} is already resolved.")

        ts = now_iso()
        c.execute(
            "UPDATE blockers SET status = 'resolved', resolved_at = ?, "
            "resolution = ? WHERE id = ? AND agent_id = ?",
            (ts, resolution, blocker_id, agent_id),
        )
        _insert_blocker_note(
            c, agent_id, blocker_id, f"Blocker resolved: {existing['title']}"
        )
        return c.execute(
            f"SELECT {_BLOCKER_COLUMNS} FROM blockers WHERE id = ?",
            (blocker_id,),
        ).fetchone()

    row = run_in_write_txn(conn, _work)
    return _row_to_blocker(row, full=True)


def escalate_blocker(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    blocker_id: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Escalate one of the agent's active blockers and auto-log a note.

    Moves an ``active`` blocker to ``escalated`` (stamping ``escalated_at``) so
    it stands out in ``blocker_list`` filters and the ``describe_agent`` active
    count. Raises if the blocker is missing, already ``resolved``, or already
    ``escalated`` (escalation is a one-way, deliberate signal — not idempotent
    churn). ``reason`` is optional context folded into the timeline note.
    """
    if reason is not None and len(reason) > _MAX_RESOLUTION_LEN:
        raise ValueError(f"reason is too long (max {_MAX_RESOLUTION_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> sqlite3.Row:
        agent_id = require_agent_id(c, canonical)
        existing = c.execute(
            "SELECT id, title, status FROM blockers WHERE id = ? AND agent_id = ?",
            (blocker_id, agent_id),
        ).fetchone()
        if existing is None:
            raise ValueError(f"blocker {blocker_id!r} not found for this agent.")
        if existing["status"] == "resolved":
            raise ValueError(f"blocker {blocker_id!r} is already resolved.")
        if existing["status"] == "escalated":
            raise ValueError(f"blocker {blocker_id!r} is already escalated.")

        ts = now_iso()
        c.execute(
            "UPDATE blockers SET status = 'escalated', escalated_at = ? "
            "WHERE id = ? AND agent_id = ?",
            (ts, blocker_id, agent_id),
        )
        note = f"Blocker escalated: {existing['title']}"
        if reason:
            note = f"{note} — {reason}"
        _insert_blocker_note(c, agent_id, blocker_id, note)
        return c.execute(
            f"SELECT {_BLOCKER_COLUMNS} FROM blockers WHERE id = ?",
            (blocker_id,),
        ).fetchone()

    row = run_in_write_txn(conn, _work)
    return _row_to_blocker(row, full=True)


def register_blocker_tools(mcp: FastMCP) -> None:
    """Register the blocker tools on the FastMCP server."""

    @mcp.tool()
    def blocker_open(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        title: Annotated[
            str,
            Field(description="Short summary of what is blocked (non-empty)."),
        ],
        description: Annotated[
            str | None,
            Field(description="Optional longer detail."),
        ] = None,
        owner: Annotated[
            str | None,
            Field(description="Optional person/team responsible for unblocking."),
        ] = None,
        workstream: Annotated[
            str | None,
            Field(description="Optional workstream/grouping label."),
        ] = None,
        external_key: Annotated[
            str | None,
            Field(
                description="Optional caller-supplied idempotency key, unique "
                "per agent. Opening a second blocker with the same key raises."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Open a persistent blocker (starts in status "active").

        Also appends a related note to your memory so the blocker shows up in
        your timeline.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            title: Short summary of what is blocked (non-empty).
            description: Optional longer detail.
            owner: Optional person/team responsible for unblocking.
            workstream: Optional workstream/grouping label.
            external_key: Optional caller-supplied idempotency key, unique per
                agent. Opening a second blocker with the same key raises.

        Returns:
            The new blocker's ``id``, ``title``, ``status``, ``raised_at`` and
            ``external_key``.
        """
        with open_connection() as conn:
            return open_blocker(
                conn,
                agent_cwd=agent_cwd,
                title=title,
                description=description,
                owner=owner,
                workstream=workstream,
                external_key=external_key,
            )

    @mcp.tool()
    def blocker_list(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        limit: Annotated[
            int, Field(description="Max blockers to return (server-capped at 200).")
        ],
        status: Annotated[
            _StatusLiteral | None,
            Field(
                description='Optional filter — "active", "escalated", or "resolved".'
            ),
        ] = None,
        workstream: Annotated[
            str | None, Field(description="Optional exact-workstream filter.")
        ] = None,
        cursor: Annotated[
            str | None,
            Field(description="Opaque token from a previous call's next_cursor."),
        ] = None,
        full: Annotated[
            bool, Field(description="Return untruncated description when true.")
        ] = False,
    ) -> dict[str, Any]:
        """List your blockers, newest first, with keyset pagination.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max blockers to return (server-capped).
            status: Optional filter — "active", "escalated", or "resolved".
            workstream: Optional exact-workstream filter.
            cursor: Opaque token from a previous call's ``next_cursor``.
            full: Return untruncated ``description`` when true.

        Returns:
            ``blockers`` (description truncated unless ``full``), ``count`` and
            ``next_cursor`` (null when there is no further page).
        """
        with open_connection() as conn:
            return list_blockers(
                conn,
                agent_cwd=agent_cwd,
                limit=limit,
                status=status,
                workstream=workstream,
                cursor=cursor,
                full=full,
            )

    @mcp.tool()
    def blocker_resolve(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        blocker_id: Annotated[
            str,
            Field(description="The id returned by blocker_open or blocker_list."),
        ],
        resolution: Annotated[
            str | None,
            Field(description="Optional note on how it was resolved."),
        ] = None,
    ) -> dict[str, Any]:
        """Resolve one of your blockers and auto-log a note.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            blocker_id: The id returned by ``blocker_open`` / ``blocker_list``.
            resolution: Optional note on how it was resolved.

        Returns:
            The full resolved blocker.
        """
        with open_connection() as conn:
            return resolve_blocker(
                conn,
                agent_cwd=agent_cwd,
                blocker_id=blocker_id,
                resolution=resolution,
            )

    @mcp.tool()
    def blocker_escalate(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        blocker_id: Annotated[
            str,
            Field(description="The id returned by blocker_open or blocker_list."),
        ],
        reason: Annotated[
            str | None,
            Field(description="Optional context on why it is being escalated."),
        ] = None,
    ) -> dict[str, Any]:
        """Escalate one of your active blockers and auto-log a note.

        Use when a blocker needs attention beyond the current agent (e.g. it is
        stuck and someone must intervene). Moves it from "active" to "escalated"
        so it stands out in filters and the describe summary. Fails if the
        blocker is already resolved or already escalated.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            blocker_id: The id returned by ``blocker_open`` / ``blocker_list``.
            reason: Optional context on why it is being escalated.

        Returns:
            The full escalated blocker.
        """
        with open_connection() as conn:
            return escalate_blocker(
                conn,
                agent_cwd=agent_cwd,
                blocker_id=blocker_id,
                reason=reason,
            )
