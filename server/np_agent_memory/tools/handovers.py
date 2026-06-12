"""Handover tools: agent-side save/read and consumer-side two-phase claim/ack.

A handover is a full structured session summary. Agents call ``handover_save``
at session end (scoped to the calling agent via ``agent_cwd``) and read their
own back with ``handover_latest`` / ``handover_export``.

The **consumer-side** tools (``handover_claim`` / ``handover_ack`` /
``handover_release``) are the transport for an external ingest process (e.g.
the Connects ``ingest-handovers`` skill). They are NOT agent-scoped: they
operate across every agent's handovers and identify the caller by an opaque
``consumer_id`` label (stored in ``claimed_by``), never by ``agent_cwd``.

The claim/ack split exists so the consumer can crash between reading and
persisting without losing data: a claim that is never acked goes stale after
``stale_minutes`` and becomes claimable again. Never collapse this into a
single "read + mark consumed" call.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory.db import open_connection, run_in_read_txn, run_in_write_txn
from np_agent_memory.identity import canonicalize_agent_cwd, new_ulid, now_iso
from np_agent_memory.tools._common import (
    clamp_limit,
    require_agent_id,
    truncate,
)


def _metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object (mapping), not a scalar/array.")
    return json.dumps(metadata, separators=(",", ":"))


def _json_or_none(value: str | None) -> Any:
    """Parse a stored JSON string, or return None."""
    return json.loads(value) if value else None


_MAX_SUMMARY_LEN = 1_024
_MAX_BODY_LEN = 1_000_000
_MAX_SESSION_LEN = 256
_MAX_CONSUMER_LEN = 128
_MAX_ERROR_LEN = 4_096

# handover_latest clips body_md to this many chars unless full=True.
_BODY_PREVIEW_LEN = 2_000

# Default / cap on how long a claim may sit unacked before it is reclaimable.
_DEFAULT_STALE_MINUTES = 15
_MAX_STALE_MINUTES = 1_440  # 24h

_HANDOVER_COLUMNS = (
    "id, agent_id, session_id, saved_at, summary, body_md, "
    "claimed_at, claimed_by, attempt_count, last_error, consumed_at, metadata_json"
)


def _row_to_handover(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    """Shape a handovers row for an agent-side response, truncating body_md."""
    body = row["body_md"]
    truncated = False
    if not full:
        body, truncated = truncate(body, _BODY_PREVIEW_LEN)
    handover: dict[str, Any] = {
        "id": row["id"],
        "session_id": row["session_id"],
        "saved_at": row["saved_at"],
        "summary": row["summary"],
        "body_md": body,
        "consumed_at": row["consumed_at"],
        "metadata": _json_or_none(row["metadata_json"]),
    }
    if truncated:
        handover["body_truncated"] = True
        handover["body_length"] = len(row["body_md"])
    return handover


def save_handover(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    summary: str,
    body_md: str,
    session_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save a full handover for the calling agent and return its id + saved_at."""
    if not summary or not summary.strip():
        raise ValueError("summary must be a non-empty, non-whitespace string.")
    if len(summary) > _MAX_SUMMARY_LEN:
        raise ValueError(f"summary is too long (max {_MAX_SUMMARY_LEN} chars).")
    if not body_md or not body_md.strip():
        raise ValueError("body_md must be a non-empty, non-whitespace string.")
    if len(body_md) > _MAX_BODY_LEN:
        raise ValueError(f"body_md is too long (max {_MAX_BODY_LEN} chars).")
    if session_id is not None and len(session_id) > _MAX_SESSION_LEN:
        raise ValueError(f"session_id is too long (max {_MAX_SESSION_LEN} chars).")

    canonical = canonicalize_agent_cwd(agent_cwd)
    metadata_json = _metadata_to_json(metadata)

    def _work(c: sqlite3.Connection) -> tuple[str, str]:
        agent_id = require_agent_id(c, canonical)
        handover_id = new_ulid()
        ts = now_iso()
        c.execute(
            "INSERT INTO handovers "
            "(id, agent_id, session_id, saved_at, summary, body_md, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (handover_id, agent_id, session_id, ts, summary, body_md, metadata_json),
        )
        return handover_id, ts

    handover_id, ts = run_in_write_txn(conn, _work)
    return {"id": handover_id, "saved_at": ts, "summary": summary}


def _latest_row(
    conn: sqlite3.Connection, *, canonical: str, handover_id: str | None
) -> sqlite3.Row | None:
    """Fetch one handover for the agent: a specific id, or the most recent."""

    def _work(c: sqlite3.Connection) -> sqlite3.Row | None:
        agent_id = require_agent_id(c, canonical)
        if handover_id is not None:
            return c.execute(
                f"SELECT {_HANDOVER_COLUMNS} FROM handovers "
                f"WHERE id = ? AND agent_id = ?",
                (handover_id, agent_id),
            ).fetchone()
        return c.execute(
            f"SELECT {_HANDOVER_COLUMNS} FROM handovers WHERE agent_id = ? "
            f"ORDER BY saved_at DESC, id DESC LIMIT 1",
            (agent_id,),
        ).fetchone()

    return run_in_read_txn(conn, _work)


def latest_handover(
    conn: sqlite3.Connection, *, agent_cwd: str, full: bool = False
) -> dict[str, Any]:
    """Return the agent's most recent handover (or ``None``)."""
    canonical = canonicalize_agent_cwd(agent_cwd)
    row = _latest_row(conn, canonical=canonical, handover_id=None)
    return {"handover": _row_to_handover(row, full=full) if row else None}


def export_handover(
    conn: sqlite3.Connection,
    *,
    agent_cwd: str,
    handover_id: str | None = None,
) -> dict[str, Any]:
    """Render one of the agent's handovers (specific id, or latest) as markdown."""
    canonical = canonicalize_agent_cwd(agent_cwd)
    row = _latest_row(conn, canonical=canonical, handover_id=handover_id)
    if row is None:
        target = f"id {handover_id!r}" if handover_id else "latest"
        raise ValueError(f"no handover found for this agent ({target}).")

    lines = [
        f"# Handover — {row['saved_at']}",
        "",
        f"**Summary:** {row['summary']}",
        "",
        row["body_md"],
        "",
    ]
    return {
        "markdown": "\n".join(lines),
        "id": row["id"],
        "saved_at": row["saved_at"],
    }


# ---------------------------------------------------------------------------
# Consumer-side: two-phase claim / ack (NOT agent-scoped; cross-agent ingest)
# ---------------------------------------------------------------------------


def _require_consumer_id(consumer_id: str) -> str:
    if not consumer_id or not consumer_id.strip():
        raise ValueError("consumer_id must be a non-empty, non-whitespace string.")
    if len(consumer_id) > _MAX_CONSUMER_LEN:
        raise ValueError(f"consumer_id is too long (max {_MAX_CONSUMER_LEN} chars).")
    return consumer_id


def _claimed_row(row: sqlite3.Row) -> dict[str, Any]:
    """Shape a claimed handover for the consumer (full body, with agent_name).

    This is the cross-agent *ingest* boundary (e.g. Connects), not the
    agent-facing boundary, so exposing the internal ``agent_id`` here is
    deliberate: the trusted consumer uses it as a stable correlation key
    alongside the human-readable ``agent_name``. Normal agent-scoped tools
    never leak this id.
    """
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "session_id": row["session_id"],
        "saved_at": row["saved_at"],
        "summary": row["summary"],
        "body_md": row["body_md"],
        "attempt_count": row["attempt_count"],
        "claimed_at": row["claimed_at"],
        "claimed_by": row["claimed_by"],
        "metadata": _json_or_none(row["metadata_json"]),
    }


def claim_handovers(
    conn: sqlite3.Connection,
    *,
    consumer_id: str,
    limit: int,
    stale_minutes: int = _DEFAULT_STALE_MINUTES,
) -> dict[str, Any]:
    """Claim up to ``limit`` unconsumed handovers for a consumer.

    Claimable = not yet consumed AND (never claimed OR the existing claim is
    older than ``stale_minutes``). Each claimed row's ``claimed_at`` /
    ``claimed_by`` are stamped and ``attempt_count`` is incremented. Oldest
    handovers (by ``saved_at``) are claimed first.
    """
    consumer_id = _require_consumer_id(consumer_id)
    limit = clamp_limit(limit)
    if not isinstance(stale_minutes, int) or isinstance(stale_minutes, bool):
        raise ValueError("stale_minutes must be an integer.")
    if stale_minutes < 0:
        raise ValueError("stale_minutes must be >= 0.")
    stale_minutes = min(stale_minutes, _MAX_STALE_MINUTES)

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        now = now_iso()
        cutoff = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).isoformat()
        candidates = c.execute(
            "SELECT id FROM handovers "
            "WHERE consumed_at IS NULL "
            "  AND (claimed_at IS NULL OR claimed_at < ?) "
            "ORDER BY saved_at ASC, id ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        ids = [r["id"] for r in candidates]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        c.execute(
            f"UPDATE handovers SET claimed_at = ?, claimed_by = ?, "
            f"attempt_count = attempt_count + 1 WHERE id IN ({placeholders})",
            [now, consumer_id, *ids],
        )
        return c.execute(
            f"SELECT h.{', h.'.join(_HANDOVER_COLUMNS.split(', '))}, "
            f"a.name AS agent_name "
            f"FROM handovers h JOIN agents a ON a.id = h.agent_id "
            f"WHERE h.id IN ({placeholders}) "
            f"ORDER BY h.saved_at ASC, h.id ASC",
            ids,
        ).fetchall()

    rows = run_in_write_txn(conn, _work)
    handovers = [_claimed_row(r) for r in rows]
    return {"handovers": handovers, "count": len(handovers)}


def ack_handovers(
    conn: sqlite3.Connection, *, consumer_id: str, ids: list[str]
) -> dict[str, Any]:
    """Mark claimed handovers consumed. Only the claiming consumer may ack."""
    consumer_id = _require_consumer_id(consumer_id)
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list of handover ids.")

    def _work(c: sqlite3.Connection) -> list[str]:
        now = now_iso()
        acked: list[str] = []
        for hid in ids:
            cur = c.execute(
                "UPDATE handovers SET consumed_at = ? "
                "WHERE id = ? AND claimed_by = ? AND consumed_at IS NULL",
                (now, hid, consumer_id),
            )
            if cur.rowcount:
                acked.append(hid)
        return acked

    acked = run_in_write_txn(conn, _work)
    skipped = [hid for hid in ids if hid not in acked]
    return {"acked": len(acked), "acked_ids": acked, "skipped": skipped}


def release_handovers(
    conn: sqlite3.Connection,
    *,
    consumer_id: str,
    ids: list[str],
    last_error: str | None = None,
) -> dict[str, Any]:
    """Release claims (clean backoff) so other consumers may retry them."""
    consumer_id = _require_consumer_id(consumer_id)
    if not isinstance(ids, list) or not ids:
        raise ValueError("ids must be a non-empty list of handover ids.")
    if last_error is not None and len(last_error) > _MAX_ERROR_LEN:
        raise ValueError(f"last_error is too long (max {_MAX_ERROR_LEN} chars).")

    def _work(c: sqlite3.Connection) -> list[str]:
        released: list[str] = []
        for hid in ids:
            cur = c.execute(
                "UPDATE handovers SET claimed_at = NULL, claimed_by = NULL, "
                "last_error = ? "
                "WHERE id = ? AND claimed_by = ? AND consumed_at IS NULL",
                (last_error, hid, consumer_id),
            )
            if cur.rowcount:
                released.append(hid)
        return released

    released = run_in_write_txn(conn, _work)
    skipped = [hid for hid in ids if hid not in released]
    return {"released": len(released), "released_ids": released, "skipped": skipped}


def register_handover_tools(mcp: FastMCP) -> None:
    """Register the handover tools (agent-side + consumer-side) on the server."""

    @mcp.tool()
    def handover_save(
        agent_cwd: str,
        summary: str,
        body_md: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save a full session handover (replaces writing a markdown file).

        Args:
            agent_cwd: Your absolute repository root (as registered).
            summary: One-line summary of the session.
            body_md: The full structured handover body (markdown).
            session_id: Optional id of the session being handed over.
            metadata: Optional JSON object of structured extras.

        Returns:
            The new handover's ``id``, ``saved_at`` and ``summary``.
        """
        with open_connection() as conn:
            return save_handover(
                conn,
                agent_cwd=agent_cwd,
                summary=summary,
                body_md=body_md,
                session_id=session_id,
                metadata=metadata,
            )

    @mcp.tool()
    def handover_latest(agent_cwd: str, full: bool = False) -> dict[str, Any]:
        """Return your most recent handover (or null if none).

        Args:
            agent_cwd: Your absolute repository root (as registered).
            full: Return untruncated ``body_md`` when true.

        Returns:
            ``handover`` — the latest handover (body truncated unless ``full``),
            or null.
        """
        with open_connection() as conn:
            return latest_handover(conn, agent_cwd=agent_cwd, full=full)

    @mcp.tool()
    def handover_export(
        agent_cwd: str, handover_id: str | None = None
    ) -> dict[str, Any]:
        """Render one of your handovers as markdown (full body).

        Args:
            agent_cwd: Your absolute repository root (as registered).
            handover_id: A specific handover id; omit for your latest.

        Returns:
            ``markdown`` (full body), the ``id`` and ``saved_at``.
        """
        with open_connection() as conn:
            return export_handover(conn, agent_cwd=agent_cwd, handover_id=handover_id)

    @mcp.tool()
    def handover_claim(
        consumer_id: str,
        limit: int,
        stale_minutes: int = _DEFAULT_STALE_MINUTES,
    ) -> dict[str, Any]:
        """Consumer-side: claim a batch of unconsumed handovers for ingest.

        Stamps ``claimed_at`` / ``claimed_by`` and increments ``attempt_count``.
        A claim older than ``stale_minutes`` is reclaimable by anyone, so an
        ingest crash between claim and ``handover_ack`` never loses data. Pair
        every claim with ``handover_ack`` (success) or ``handover_release``
        (backoff).

        Args:
            consumer_id: Opaque label for the ingesting process (e.g.
                "connects-ingest").
            limit: Max handovers to claim (server-capped).
            stale_minutes: How long an existing claim must be before it is
                reclaimable (default 15, capped at 1440).

        Returns:
            ``handovers`` (full body, with ``agent_name``) and ``count``.
        """
        with open_connection() as conn:
            return claim_handovers(
                conn,
                consumer_id=consumer_id,
                limit=limit,
                stale_minutes=stale_minutes,
            )

    @mcp.tool()
    def handover_ack(consumer_id: str, ids: list[str]) -> dict[str, Any]:
        """Consumer-side: mark claimed handovers as consumed.

        Only handovers currently claimed by ``consumer_id`` (and not already
        consumed) are acked; everything else is reported in ``skipped``.

        Args:
            consumer_id: The same label used to claim.
            ids: Handover ids to mark consumed.

        Returns:
            ``acked`` count, ``acked_ids`` and ``skipped`` ids.
        """
        with open_connection() as conn:
            return ack_handovers(conn, consumer_id=consumer_id, ids=ids)

    @mcp.tool()
    def handover_release(
        consumer_id: str, ids: list[str], last_error: str | None = None
    ) -> dict[str, Any]:
        """Consumer-side: release claims so they can be retried later.

        Clears ``claimed_at`` / ``claimed_by`` and records ``last_error`` for
        handovers currently claimed by ``consumer_id`` and not yet consumed.

        Args:
            consumer_id: The same label used to claim.
            ids: Handover ids to release.
            last_error: Optional reason recorded on each released handover.

        Returns:
            ``released`` count, ``released_ids`` and ``skipped`` ids.
        """
        with open_connection() as conn:
            return release_handovers(
                conn, consumer_id=consumer_id, ids=ids, last_error=last_error
            )
