"""Shared helpers for the tool layer: identity resolution, pagination, limits.

These back the memory/todos (and later blocker/handover/inbox) tools so the
keyset-pagination, limit-capping, cursor, and agent-resolution logic lives in
one place. Pure helpers — they take a ``sqlite3.Connection`` (or plain values)
and never open their own connection.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from typing import Any

# Server-side bounds on list/query ``limit``. ``limit`` is required on every
# list tool; values are clamped into this range so a caller cannot request an
# unbounded scan or a non-positive page.
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def resolve_agent_id(conn: sqlite3.Connection, canonical_path: str) -> str | None:
    """Return the internal agent ULID for a canonical path, or ``None``.

    The single source of truth for "which agent owns this working directory".
    Callers run this inside their own transaction so the lookup shares the
    snapshot of the surrounding read/write unit.
    """
    row = conn.execute(
        "SELECT agent_id FROM agent_aliases WHERE alias_path = ?",
        (canonical_path,),
    ).fetchone()
    return row["agent_id"] if row is not None else None


def require_agent_id(conn: sqlite3.Connection, canonical_path: str) -> str:
    """Like :func:`resolve_agent_id` but raise if the path is unregistered.

    Used by tools that assume the agent registered at session start. Raising
    (rather than returning empty results) ensures a typo'd or stale ``agent_cwd``
    surfaces loudly instead of masquerading as "no data".
    """
    agent_id = resolve_agent_id(conn, canonical_path)
    if agent_id is None:
        raise ValueError(
            f"agent_cwd is not registered: {canonical_path!r}. "
            f"Call agent_register first."
        )
    return agent_id


def clamp_limit(limit: int) -> int:
    """Validate and cap a caller-supplied ``limit`` into ``[1, MAX_LIMIT]``."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an integer.")
    if limit < 1:
        raise ValueError("limit must be >= 1.")
    return min(limit, MAX_LIMIT)


def encode_cursor(values: list[Any]) -> str:
    """Encode an ordering key into an opaque, URL-safe pagination cursor."""
    raw = json.dumps(values, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str) -> list[Any]:
    """Decode a cursor produced by :func:`encode_cursor`.

    Raises ``ValueError`` on any malformed token so a corrupt/forged cursor is
    a clean caller error, not an internal traceback.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        values = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor: {token!r}.") from exc
    if not isinstance(values, list):
        raise ValueError(f"invalid cursor: {token!r}.")
    return values


def keyset_predicate(
    keys: list[tuple[str, Any]], *, direction: str = "<"
) -> tuple[str, list[Any]]:
    """Build a keyset-pagination WHERE fragment for a composite ordering key.

    Given ordered ``(sql_expr, value)`` pairs ``[(a, va), (b, vb), (c, vc)]``
    and ``direction`` ``"<"`` (descending order / "rows after this one"),
    returns the lexicographic comparison::

        (a < va) OR (a = va AND b < vb) OR (a = va AND b = vb AND c < vc)

    The ``sql_expr`` entries are interpolated verbatim (they are column names or
    fixed CASE expressions controlled by the caller, never user input); the
    values are returned as bound parameters.
    """
    if direction not in ("<", ">"):
        raise ValueError("direction must be '<' or '>'.")
    if not keys:
        raise ValueError("keyset_predicate requires at least one key.")

    ors: list[str] = []
    params: list[Any] = []
    for i in range(len(keys)):
        terms: list[str] = []
        for j in range(i):
            expr, value = keys[j]
            terms.append(f"{expr} = ?")
            params.append(value)
        expr, value = keys[i]
        terms.append(f"{expr} {direction} ?")
        params.append(value)
        ors.append("(" + " AND ".join(terms) + ")")

    return "(" + " OR ".join(ors) + ")", params


def truncate(text: str, length: int) -> tuple[str, bool]:
    """Return ``(preview, was_truncated)`` clipping ``text`` to ``length`` chars."""
    if len(text) <= length:
        return text, False
    return text[:length], True
