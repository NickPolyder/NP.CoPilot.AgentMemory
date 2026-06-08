"""Todo tools: create, list, and update long-running todos.

Todos are durable, cross-session work items scoped to the calling agent via
``agent_cwd`` (see ``tools/agents.py`` for the identity/trust model).
``todo_add`` creates; ``todo_list`` reads a keyset-paginated window;
``todo_update`` mutates status/priority/due/description.
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

# Mirrors the CHECK constraints on todos.status / todos.priority.
_STATUSES = ("pending", "in_progress", "done", "blocked", "cancelled")
_PRIORITIES = ("low", "normal", "high", "urgent")

# Ordinal mapping for ordered listing. The text priority column sorts
# alphabetically (high < low < normal < urgent), which is meaningless; ordered
# listing must rank by this ordinal instead (see docs/PLAN.md phase-3 caveat).
_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}
# SQL CASE that reproduces _PRIORITY_RANK as an integer column for ORDER BY and
# keyset comparison. Fixed expression (no user input interpolated).
_PRIORITY_RANK_SQL = (
    "CASE priority WHEN 'low' THEN 0 WHEN 'normal' THEN 1 "
    "WHEN 'high' THEN 2 WHEN 'urgent' THEN 3 ELSE 1 END"
)

_SORTS = ("recent", "priority")

_MAX_TITLE_LEN = 256
_MAX_DESCRIPTION_LEN = 8_192
_MAX_DUE_LEN = 64

# todo_list clips description to this many chars unless full=True.
_DESCRIPTION_PREVIEW_LEN = 500

_TODO_COLUMNS = (
    "id, title, description, status, priority, due_date, "
    "created_at, updated_at, completed_at, metadata_json"
)


def _metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object (mapping), not a scalar/array.")
    return json.dumps(metadata, separators=(",", ":"))


def _row_to_todo(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    description = row["description"]
    truncated = False
    if not full and description is not None:
        description, truncated = truncate(description, _DESCRIPTION_PREVIEW_LEN)
    todo: dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "description": description,
        "status": row["status"],
        "priority": row["priority"],
        "due_date": row["due_date"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
    }
    if truncated:
        todo["description_truncated"] = True
        todo["description_length"] = len(row["description"])
    return todo


def add_todo(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    title: str,
    description: str | None = None,
    priority: str = "normal",
    due_date: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a long-running todo for the calling agent (status ``pending``)."""
    if not title or not title.strip():
        raise ValueError("title must be a non-empty, non-whitespace string.")
    if len(title) > _MAX_TITLE_LEN:
        raise ValueError(f"title is too long (max {_MAX_TITLE_LEN} chars).")
    if priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}.")
    if description is not None and len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(f"description is too long (max {_MAX_DESCRIPTION_LEN} chars).")
    if due_date is not None and len(due_date) > _MAX_DUE_LEN:
        raise ValueError(f"due_date is too long (max {_MAX_DUE_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)
    metadata_json = _metadata_to_json(metadata)

    def _work(c: sqlite3.Connection) -> tuple[str, str]:
        agent_id = require_agent_id(c, canonical)
        todo_id = new_ulid()
        ts = now_iso()
        c.execute(
            "INSERT INTO todos "
            "(id, agent_id, title, description, status, priority, due_date, "
            "created_at, updated_at, metadata_json) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (
                todo_id,
                agent_id,
                title,
                description,
                priority,
                due_date,
                ts,
                ts,
                metadata_json,
            ),
        )
        return todo_id, ts

    todo_id, ts = run_in_write_txn(conn, _work)
    return {
        "id": todo_id,
        "title": title,
        "status": "pending",
        "priority": priority,
        "created_at": ts,
    }


def list_todos(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    limit: int,
    status: str | None = None,
    priority: str | None = None,
    sort: str = "recent",
    cursor: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Return a keyset-paginated window of the agent's todos.

    ``sort="recent"`` orders by ``created_at`` desc; ``sort="priority"`` orders
    by the priority *ordinal* (urgent first) then ``created_at`` desc.
    """
    limit = clamp_limit(limit)
    if status is not None and status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, got {status!r}.")
    if priority is not None and priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}.")
    if sort not in _SORTS:
        raise ValueError(f"sort must be one of {_SORTS}, got {sort!r}.")

    canonical = canonicalize_agent_cwd(agent_cwd)
    cursor_key = decode_cursor(cursor) if cursor else None

    if sort == "priority":
        order_by = f"{_PRIORITY_RANK_SQL} DESC, created_at DESC, id DESC"
        expected_cursor_len = 3
    else:
        order_by = "created_at DESC, id DESC"
        expected_cursor_len = 2
    if cursor_key is not None and len(cursor_key) != expected_cursor_len:
        raise ValueError("cursor does not match the requested sort.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        agent_id = require_agent_id(c, canonical)
        clauses = ["agent_id = ?"]
        params: list[Any] = [agent_id]
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if priority is not None:
            clauses.append("priority = ?")
            params.append(priority)
        if cursor_key is not None:
            if sort == "priority":
                keys = [
                    (_PRIORITY_RANK_SQL, cursor_key[0]),
                    ("created_at", cursor_key[1]),
                    ("id", cursor_key[2]),
                ]
            else:
                keys = [("created_at", cursor_key[0]), ("id", cursor_key[1])]
            frag, frag_params = keyset_predicate(keys, direction="<")
            clauses.append(frag)
            params.extend(frag_params)
        params.append(limit + 1)
        return c.execute(
            f"SELECT {_TODO_COLUMNS} FROM todos WHERE {' AND '.join(clauses)} "
            f"ORDER BY {order_by} LIMIT ?",
            params,
        ).fetchall()

    rows = run_in_read_txn(conn, _work)
    has_more = len(rows) > limit
    rows = rows[:limit]
    todos = [_row_to_todo(r, full=full) for r in rows]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        if sort == "priority":
            next_cursor = encode_cursor(
                [_PRIORITY_RANK[last["priority"]], last["created_at"], last["id"]]
            )
        else:
            next_cursor = encode_cursor([last["created_at"], last["id"]])
    return {"todos": todos, "count": len(todos), "next_cursor": next_cursor}


def update_todo(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    todo_id: str,
    status: str | None = None,
    priority: str | None = None,
    due_date: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update mutable fields of one of the agent's todos.

    At least one field must be supplied. Setting ``status="done"`` stamps
    ``completed_at``; moving to any other status clears it. The todo must belong
    to the calling agent.
    """
    if status is None and priority is None and due_date is None and description is None:
        raise ValueError(
            "Provide at least one of status, priority, due_date, description."
        )
    if status is not None and status not in _STATUSES:
        raise ValueError(f"status must be one of {_STATUSES}, got {status!r}.")
    if priority is not None and priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}.")
    if description is not None and len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(f"description is too long (max {_MAX_DESCRIPTION_LEN} chars).")
    if due_date is not None and len(due_date) > _MAX_DUE_LEN:
        raise ValueError(f"due_date is too long (max {_MAX_DUE_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> sqlite3.Row:
        agent_id = require_agent_id(c, canonical)
        existing = c.execute(
            "SELECT id FROM todos WHERE id = ? AND agent_id = ?",
            (todo_id, agent_id),
        ).fetchone()
        if existing is None:
            raise ValueError(f"todo {todo_id!r} not found for this agent.")

        ts = now_iso()
        sets = ["updated_at = ?"]
        params: list[Any] = [ts]
        if status is not None:
            sets.append("status = ?")
            params.append(status)
            # done -> stamp completion; any other status -> clear it.
            if status == "done":
                sets.append("completed_at = ?")
                params.append(ts)
            else:
                sets.append("completed_at = NULL")
        if priority is not None:
            sets.append("priority = ?")
            params.append(priority)
        if due_date is not None:
            sets.append("due_date = ?")
            params.append(due_date)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        params.extend([todo_id, agent_id])
        c.execute(
            f"UPDATE todos SET {', '.join(sets)} WHERE id = ? AND agent_id = ?",
            params,
        )
        return c.execute(
            f"SELECT {_TODO_COLUMNS} FROM todos WHERE id = ?",
            (todo_id,),
        ).fetchone()

    row = run_in_write_txn(conn, _work)
    return _row_to_todo(row, full=True)


def register_todo_tools(mcp: FastMCP) -> None:
    """Register the todo tools on the FastMCP server."""

    @mcp.tool()
    def todo_add(
        agent_cwd: str,
        title: str,
        description: str | None = None,
        priority: str = "normal",
        due_date: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a long-running todo (starts in status "pending").

        Args:
            agent_cwd: Your absolute repository root (as registered).
            title: Short summary of the work (non-empty).
            description: Optional longer detail.
            priority: One of "low", "normal", "high", "urgent" (default normal).
            due_date: Optional ISO-8601 due date/time.
            metadata: Optional JSON object of structured extras.

        Returns:
            The new todo's ``id``, ``title``, ``status``, ``priority`` and
            ``created_at``.
        """
        with open_connection() as conn:
            return add_todo(
                conn,
                agent_cwd=agent_cwd,
                title=title,
                description=description,
                priority=priority,
                due_date=due_date,
                metadata=metadata,
            )

    @mcp.tool()
    def todo_list(
        agent_cwd: str,
        limit: int,
        status: str | None = None,
        priority: str | None = None,
        sort: str = "recent",
        cursor: str | None = None,
        full: bool = False,
    ) -> dict[str, Any]:
        """List your todos with keyset pagination.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max todos to return (server-capped).
            status: Optional filter — pending/in_progress/done/blocked/cancelled.
            priority: Optional filter — low/normal/high/urgent.
            sort: "recent" (default, newest first) or "priority" (urgent first).
            cursor: Opaque token from a previous call's ``next_cursor`` (must
                match the same ``sort``).
            full: Return untruncated ``description`` when true.

        Returns:
            ``todos`` (description truncated unless ``full``), ``count`` and
            ``next_cursor`` (null when there is no further page).
        """
        with open_connection() as conn:
            return list_todos(
                conn,
                agent_cwd=agent_cwd,
                limit=limit,
                status=status,
                priority=priority,
                sort=sort,
                cursor=cursor,
                full=full,
            )

    @mcp.tool()
    def todo_update(
        agent_cwd: str,
        todo_id: str,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Update one of your todos. Provide at least one field to change.

        Setting ``status="done"`` stamps ``completed_at``; any other status
        clears it.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            todo_id: The id returned by ``todo_add`` / ``todo_list``.
            status: New status (pending/in_progress/done/blocked/cancelled).
            priority: New priority (low/normal/high/urgent).
            due_date: New ISO-8601 due date/time.
            description: New description.

        Returns:
            The full updated todo.
        """
        with open_connection() as conn:
            return update_todo(
                conn,
                agent_cwd=agent_cwd,
                todo_id=todo_id,
                status=status,
                priority=priority,
                due_date=due_date,
                description=description,
            )
