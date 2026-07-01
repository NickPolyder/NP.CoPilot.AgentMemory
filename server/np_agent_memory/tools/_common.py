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

# Server-side cap on the serialized size of an agent-supplied ``metadata``
# object. Bounds local DB bloat and MCP response size; the per-field text caps
# (name/title/body/etc.) do not otherwise constrain the free-form metadata blob.
MAX_METADATA_BYTES = 8192

# Server-side cap on the number of ids accepted by a batch mutation tool
# (delete/ack/release). Mirrors ``MAX_LIMIT`` so a single call cannot build an
# unbounded ``IN (...)`` clause or an oversized write transaction.
MAX_ID_BATCH = MAX_LIMIT


def metadata_to_json(metadata: dict[str, Any] | None) -> str | None:
    """Serialize an agent-supplied ``metadata`` mapping to compact JSON, or ``None``.

    Shared by every tool that persists a ``metadata`` blob. Rejects non-mappings,
    non-finite floats (``NaN``/``Infinity`` are not valid JSON), and payloads
    whose serialized form exceeds :data:`MAX_METADATA_BYTES` so a pathological
    caller cannot bloat the DB or an MCP response.
    """
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object (mapping), not a scalar/array.")
    try:
        encoded = json.dumps(metadata, separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"metadata is not JSON-serializable: {exc}") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(
            f"metadata is too large (max {MAX_METADATA_BYTES} bytes serialized)."
        )
    return encoded


def validate_id_batch(
    ids: list[str], *, label: str = "ids", max_count: int = MAX_ID_BATCH
) -> list[str]:
    """Validate and de-duplicate a caller-supplied batch of ids.

    Returns the ids with order preserved and duplicates removed. Raises
    ``ValueError`` for a non-list, empty list, non-string element, or a batch
    larger than ``max_count``. ``label`` names the argument in error messages
    (e.g. ``"ids"``, ``"message_ids"``).
    """
    if not isinstance(ids, list) or not ids:
        raise ValueError(f"{label} must be a non-empty list of ids.")
    if not all(isinstance(item, str) for item in ids):
        raise ValueError(f"{label} must contain only strings.")
    if len(ids) > max_count:
        raise ValueError(f"{label} must contain at most {max_count} ids per call.")
    return list(dict.fromkeys(ids))


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
    # Every cursor element is an ordering-key value bound directly into a
    # SQLite query. Reject nested arrays/objects here so a forged-but-decodable
    # cursor is a clean ValueError instead of a sqlite3.ProgrammingError at bind
    # time. ``bool`` is excluded explicitly (it is an ``int`` subclass but never
    # a valid ordering key).
    for value in values:
        if isinstance(value, bool) or not isinstance(
            value, (str, int, float, type(None))
        ):
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
