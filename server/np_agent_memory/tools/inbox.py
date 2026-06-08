"""Inbox tools: send, check, and acknowledge cross-agent messages.

Messages are durable, agent-controlled, untrusted content. Senders and
recipients identify themselves by ``agent_cwd`` aliases (see ``tools/agents.py``
for the identity/trust model). ``inbox_check`` is strictly scoped to the calling
recipient and omits acknowledged messages, treating ack as archive/handled.
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
    resolve_agent_id,
    truncate,
)

_PRIORITIES = ("low", "normal", "high", "urgent")
_ACK_STATUSES = ("read", "acked")

_MAX_SUBJECT_LEN = 256
_MAX_BODY_LEN = 65_536
_BODY_PREVIEW_LEN = 2_000

_INBOX_COLUMNS = (
    "id, from_agent_id, from_label, subject, body, priority, "
    "sent_at, read_at, acked_at, metadata_json"
)


def _metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    """Serialize a metadata object to a compact JSON string, or ``None``."""
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object (mapping), not a scalar/array.")
    return json.dumps(metadata, separators=(",", ":"))


def _validate_send(*, subject: str, body: str, priority: str) -> None:
    if not subject or not subject.strip():
        raise ValueError("subject must be a non-empty, non-whitespace string.")
    if len(subject) > _MAX_SUBJECT_LEN:
        raise ValueError(f"subject is too long (max {_MAX_SUBJECT_LEN} chars).")
    if not body or not body.strip():
        raise ValueError("body must be a non-empty, non-whitespace string.")
    if len(body) > _MAX_BODY_LEN:
        raise ValueError(f"body is too long (max {_MAX_BODY_LEN} chars).")
    if priority not in _PRIORITIES:
        raise ValueError(f"priority must be one of {_PRIORITIES}, got {priority!r}.")


def _resolve_recipient(conn: sqlite3.Connection, to: str) -> str:
    """Resolve a recipient by canonical path first, then by unique agent name."""
    try:
        canonical_to = canonicalize_agent_cwd(to)
    except ValueError:
        canonical_to = None

    if canonical_to is not None:
        agent_id = resolve_agent_id(conn, canonical_to)
        if agent_id is not None:
            return agent_id

    rows = conn.execute(
        "SELECT id FROM agents WHERE name = ? ORDER BY id",
        (to,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["id"]
    if len(rows) > 1:
        raise ValueError(
            f"recipient name {to!r} matches multiple agents; "
            f"use the canonical path instead."
        )
    raise ValueError(f"recipient not found: {to!r}.")


def _row_to_message(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    """Shape an inbox row for a tool response, truncating body unless full."""
    body = row["body"]
    truncated = False
    if not full:
        body, truncated = truncate(body, _BODY_PREVIEW_LEN)
    message: dict[str, Any] = {
        "id": row["id"],
        "from_agent_id": row["from_agent_id"],
        "from_label": row["from_label"],
        "subject": row["subject"],
        "body": body,
        "priority": row["priority"],
        "sent_at": row["sent_at"],
        "read_at": row["read_at"],
        "acked_at": row["acked_at"],
        "metadata": json.loads(row["metadata_json"]) if row["metadata_json"] else None,
    }
    if truncated:
        message["body_truncated"] = True
        message["body_length"] = len(row["body"])
    return message


def inbox_send(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    to: str,
    subject: str,
    body: str,
    priority: str = "normal",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Send an inbox message from the calling agent to another registered agent."""
    _validate_send(subject=subject, body=body, priority=priority)
    canonical = canonicalize_agent_cwd(agent_cwd)
    metadata_json = _metadata_to_json(metadata)

    def _work(c: sqlite3.Connection) -> tuple[str, str, str]:
        sender_id = require_agent_id(c, canonical)
        sender = c.execute(
            "SELECT name FROM agents WHERE id = ?",
            (sender_id,),
        ).fetchone()
        to_agent_id = _resolve_recipient(c, to)
        message_id = new_ulid()
        ts = now_iso()
        c.execute(
            "INSERT INTO inbox "
            "(id, from_agent_id, from_label, to_agent_id, subject, body, "
            "priority, sent_at, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                sender_id,
                sender["name"],
                to_agent_id,
                subject,
                body,
                priority,
                ts,
                metadata_json,
            ),
        )
        return message_id, ts, to_agent_id

    message_id, ts, to_agent_id = run_in_write_txn(conn, _work)
    return {
        "id": message_id,
        "sent_at": ts,
        "to_agent_id": to_agent_id,
        "priority": priority,
    }


def inbox_check(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    limit: int,
    include_read: bool = False,
    cursor: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Return a keyset-paginated window of this agent's unacked messages."""
    limit = clamp_limit(limit)
    canonical = canonicalize_agent_cwd(agent_cwd)
    cursor_key = decode_cursor(cursor) if cursor else None
    if cursor_key is not None and len(cursor_key) != 2:
        raise ValueError("invalid cursor for inbox check.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        agent_id = require_agent_id(c, canonical)
        clauses = ["to_agent_id = ?", "acked_at IS NULL"]
        params: list[Any] = [agent_id]
        if not include_read:
            clauses.append("read_at IS NULL")
        if cursor_key is not None:
            frag, frag_params = keyset_predicate(
                [("sent_at", cursor_key[0]), ("id", cursor_key[1])],
                direction="<",
            )
            clauses.append(frag)
            params.extend(frag_params)
        params.append(limit + 1)
        return c.execute(
            f"SELECT {_INBOX_COLUMNS} FROM inbox WHERE {' AND '.join(clauses)} "
            f"ORDER BY sent_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()

    rows = run_in_read_txn(conn, _work)
    has_more = len(rows) > limit
    rows = rows[:limit]
    messages = [_row_to_message(row, full=full) for row in rows]
    next_cursor = (
        encode_cursor([rows[-1]["sent_at"], rows[-1]["id"]])
        if has_more and rows
        else None
    )
    return {"messages": messages, "count": len(messages), "next_cursor": next_cursor}


def inbox_ack(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    message_ids: list[str],
    status: str = "acked",
) -> dict[str, Any]:
    """Mark this agent's inbox messages as read or acknowledged/archived."""
    if status not in _ACK_STATUSES:
        raise ValueError(f"status must be one of {_ACK_STATUSES}, got {status!r}.")
    if not isinstance(message_ids, list) or not message_ids:
        raise ValueError("message_ids must be a non-empty list.")
    if not all(isinstance(message_id, str) for message_id in message_ids):
        raise ValueError("message_ids must contain only strings.")

    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> dict[str, Any]:
        agent_id = require_agent_id(c, canonical)
        marks = ", ".join("?" for _ in message_ids)
        found_rows = c.execute(
            f"SELECT id FROM inbox WHERE to_agent_id = ? AND id IN ({marks})",
            (agent_id, *message_ids),
        ).fetchall()
        found_ids = {row["id"] for row in found_rows}
        not_found = [
            message_id for message_id in message_ids if message_id not in found_ids
        ]

        ts = now_iso()
        if status == "read":
            result = c.execute(
                f"UPDATE inbox SET read_at = ? "
                f"WHERE to_agent_id = ? AND read_at IS NULL AND id IN ({marks})",
                (ts, agent_id, *message_ids),
            )
        else:
            result = c.execute(
                f"UPDATE inbox SET acked_at = ?, read_at = COALESCE(read_at, ?) "
                f"WHERE to_agent_id = ? AND id IN ({marks})",
                (ts, ts, agent_id, *message_ids),
            )
        return {"updated": result.rowcount, "not_found": not_found}

    return run_in_write_txn(conn, _work)


_send_inbox = inbox_send
_check_inbox = inbox_check
_ack_inbox = inbox_ack


def register_inbox_tools(mcp: FastMCP) -> None:
    """Register the inbox tools on the FastMCP server."""

    @mcp.tool()
    def inbox_send(
        agent_cwd: str,
        to: str,
        subject: str,
        body: str,
        priority: str = "normal",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an inbox message to another registered agent.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            to: Recipient canonical path or unique agent name.
            subject: Short message subject (non-empty).
            body: Message body (non-empty).
            priority: One of "low", "normal", "high", "urgent".
            metadata: Optional JSON object of structured extras.

        Returns:
            The new message's ``id``, ``sent_at``, ``to_agent_id`` and priority.
        """
        with open_connection() as conn:
            return _send_inbox(
                conn,
                agent_cwd=agent_cwd,
                to=to,
                subject=subject,
                body=body,
                priority=priority,
                metadata=metadata,
            )

    @mcp.tool()
    def inbox_check(
        agent_cwd: str,
        limit: int,
        include_read: bool = False,
        cursor: str | None = None,
        full: bool = False,
    ) -> dict[str, Any]:
        """List your unacked inbox messages, newest first.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            limit: Max messages to return (server-capped).
            include_read: Include read-but-unacked messages when true.
            cursor: Opaque token from a previous call's ``next_cursor``.
            full: Return untruncated ``body`` when true.

        Returns:
            ``messages`` (body truncated unless ``full``), ``count`` and
            ``next_cursor`` (null when there is no further page).
        """
        with open_connection() as conn:
            return _check_inbox(
                conn,
                agent_cwd=agent_cwd,
                limit=limit,
                include_read=include_read,
                cursor=cursor,
                full=full,
            )

    @mcp.tool()
    def inbox_ack(
        agent_cwd: str,
        message_ids: list[str],
        status: str = "acked",
    ) -> dict[str, Any]:
        """Mark your inbox messages as read or acknowledged/archived.

        Args:
            agent_cwd: Your absolute repository root (as registered).
            message_ids: Non-empty list of message ids to update.
            status: "read" or "acked" (default acked).

        Returns:
            ``updated`` count and ``not_found`` ids that do not belong to you.
        """
        with open_connection() as conn:
            return _ack_inbox(
                conn,
                agent_cwd=agent_cwd,
                message_ids=message_ids,
                status=status,
            )
