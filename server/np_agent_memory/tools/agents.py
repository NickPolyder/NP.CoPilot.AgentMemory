"""Agent-identity tools: register, describe, and alias agents.

Agents never see internal ULIDs. They re-identify on every call by passing
their canonical working directory (``agent_cwd``); the server resolves that to
an internal agent row via ``agent_aliases``. See ``np_agent_memory.identity``
for the canonicalization contract and ``docs/spike-roots.md`` for why
``agent_cwd`` is required rather than derived.

Trust model: ``agent_cwd`` is a *routing key, not authentication*. Every agent
runs as the same OS user over local stdio (see ADR 0001), so any local agent
that knows another agent's path could assert that identity. This is an accepted
assumption for the single-user, local, secret-free v1 and must be revisited
before any multi-user, cross-machine, or privileged use. Stored metadata
(``name``/``workstream``/``description`` and, later, message/handover bodies)
is agent-controlled and must be treated as untrusted by downstream renderers.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from np_agent_memory.db import open_connection, run_in_read_txn, run_in_write_txn
from np_agent_memory.identity import (
    canonicalize_agent_cwd,
    display_basename,
    new_ulid,
    now_iso,
)
from np_agent_memory.tools._common import (
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    require_agent_id,
    resolve_agent_id,
    truncate,
)

# Todo statuses that count as "still open" for the describe summary.
_OPEN_TODO_STATUSES = ("pending", "in_progress", "blocked")
# Blocker statuses that count as "still active" for the describe summary.
_ACTIVE_BLOCKER_STATUSES = ("active", "escalated")

# Server-side caps on agent-supplied identity metadata. Guard against a
# pathological caller bloating the DB / tool responses; generous vs real use.
_MAX_NAME_LEN = 128
_MAX_WORKSTREAM_LEN = 128
_MAX_DESCRIPTION_LEN = 4096
# Preview length for ``description`` in the agent directory listing. The full
# value (up to _MAX_DESCRIPTION_LEN) is only returned when the caller asks for
# ``full=True``; keeping the list preview short bounds a multi-agent response.
_DESCRIPTION_PREVIEW_LEN = 280


def _validate_metadata(
    *, name: str | None, workstream: str | None, description: str | None
) -> None:
    """Validate agent metadata at the API boundary.

    ``name`` is optional: when omitted on first registration the server defaults
    it to the working directory's name, and on re-registration an omitted name
    preserves the stored label. A *provided* name must be non-blank — a blank
    string would otherwise clobber a good label. Length caps bound stored size.
    """
    if name is not None and not name.strip():
        raise ValueError(
            "name, when provided, must be a non-empty, non-whitespace string."
        )
    if name is not None and len(name) > _MAX_NAME_LEN:
        raise ValueError(f"name is too long (max {_MAX_NAME_LEN} chars).")
    if workstream is not None and len(workstream) > _MAX_WORKSTREAM_LEN:
        raise ValueError(f"workstream is too long (max {_MAX_WORKSTREAM_LEN} chars).")
    if description is not None and len(description) > _MAX_DESCRIPTION_LEN:
        raise ValueError(f"description is too long (max {_MAX_DESCRIPTION_LEN} chars).")


def register_agent(
    conn: sqlite3.Connection,
    *,
    name: str | None = None,
    agent_cwd: str,
    workstream: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Idempotently upsert an agent and its alias for ``agent_cwd``.

    First call for a canonical path mints a new ULID + agent + alias, defaulting
    ``name`` to the working directory's own name (preserving on-disk casing)
    when none is supplied.

    The name is **sticky**: once an agent exists, re-registration never changes
    its stored ``name`` — any ``name`` passed on a repeat call is ignored (not
    an error), so agents can keep calling ``agent_register`` at session start
    without flip-flopping their identity. Renaming is an explicit, user-driven
    action via :func:`rename_agent` / the ``agent_rename`` tool.

    Repeat calls still update ``workstream`` / ``description`` *only when
    provided* (``None`` never erases a stored value) and bump ``updated_at``.
    Runs as a single ``BEGIN IMMEDIATE`` transaction, retried on lock contention
    so a concurrent first-registration race resolves to one agent.
    """
    # ``workstream``/``description`` apply on both the create and update paths, so
    # validate them up front. ``name`` is validated lazily *only* on the
    # new-agent path below: for an already-registered agent the sticky-name
    # contract ignores any supplied name, so a blank/overlong name must not
    # break an otherwise-idempotent session-start re-registration.
    _validate_metadata(name=None, workstream=workstream, description=description)
    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
        existing_agent_id = resolve_agent_id(c, canonical)
        ts = now_iso()

        if existing_agent_id is not None:
            agent_id = existing_agent_id
            # Name is sticky: an already-registered agent keeps its stored name
            # even if a (different) name is passed here. Renames are explicit,
            # via rename_agent / the agent_rename tool. This stops agents from
            # silently changing their own name on every session-start register.
            sets = ["updated_at = ?"]
            params: list[Any] = [ts]
            if workstream is not None:
                sets.append("workstream = ?")
                params.append(workstream)
            if description is not None:
                sets.append("description = ?")
                params.append(description)
            params.append(agent_id)
            c.execute(f"UPDATE agents SET {', '.join(sets)} WHERE id = ?", params)
            registered = "existing"
        else:
            _validate_metadata(name=name, workstream=None, description=None)
            agent_id = new_ulid()
            effective_name = name if name is not None else display_basename(agent_cwd)
            c.execute(
                "INSERT INTO agents "
                "(id, name, workstream, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, effective_name, workstream, description, ts, ts),
            )
            c.execute(
                "INSERT INTO agent_aliases (alias_path, agent_id, created_at) "
                "VALUES (?, ?, ?)",
                (canonical, agent_id, ts),
            )
            registered = "new"

        agent = c.execute(
            "SELECT name, workstream, description, created_at, updated_at "
            "FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()
        return registered, agent

    registered, agent = run_in_write_txn(conn, _work)
    return {
        "registered": registered,
        "name": agent["name"],
        "workstream": agent["workstream"],
        "description": agent["description"],
        "canonical_path": canonical,
        "created_at": agent["created_at"],
        "updated_at": agent["updated_at"],
    }


def describe_agent(conn: sqlite3.Connection, *, agent_cwd: str) -> dict[str, Any]:
    """Return the calling agent's metadata plus open-work counts.

    Reads run in a single deferred transaction so the alias lookup and the
    three counts share one consistent snapshot.

    Two outcome channels — callers should handle both:

    * A *valid but unregistered* path returns a soft ``{"registered": False}``
      (no exception) so a session-start probe can branch on it.
    * A *malformed* ``agent_cwd`` (empty, relative, too long, unresolvable, or
      not an existing directory) raises ``ValueError`` from canonicalization —
      it is a caller error, not an "unregistered" answer, and is surfaced so a
      typo'd or stale repo root is not silently treated as unregistered.
    """
    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> dict[str, Any] | None:
        agent_id = resolve_agent_id(c, canonical)
        if agent_id is None:
            return None

        agent = c.execute(
            "SELECT name, workstream, description, created_at, updated_at "
            "FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()

        todo_marks = ", ".join("?" for _ in _OPEN_TODO_STATUSES)
        blocker_marks = ", ".join("?" for _ in _ACTIVE_BLOCKER_STATUSES)

        unread = c.execute(
            "SELECT COUNT(*) FROM inbox WHERE to_agent_id = ? AND read_at IS NULL",
            (agent_id,),
        ).fetchone()[0]
        open_todos = c.execute(
            f"SELECT COUNT(*) FROM todos "
            f"WHERE agent_id = ? AND status IN ({todo_marks})",
            (agent_id, *_OPEN_TODO_STATUSES),
        ).fetchone()[0]
        active_blockers = c.execute(
            f"SELECT COUNT(*) FROM blockers "
            f"WHERE agent_id = ? AND status IN ({blocker_marks})",
            (agent_id, *_ACTIVE_BLOCKER_STATUSES),
        ).fetchone()[0]

        return {
            "registered": True,
            "name": agent["name"],
            "workstream": agent["workstream"],
            "description": agent["description"],
            "canonical_path": canonical,
            "created_at": agent["created_at"],
            "updated_at": agent["updated_at"],
            "unread_messages": unread,
            "open_todos": open_todos,
            "active_blockers": active_blockers,
        }

    result = run_in_read_txn(conn, _work)
    if result is None:
        return {
            "registered": False,
            "canonical_path": canonical,
            "hint": "No agent is registered for this path. Call agent_register first.",
        }
    return result


def add_alias(
    conn: sqlite3.Connection, *, agent_cwd: str, new_cwd: str
) -> dict[str, Any]:
    """Add ``new_cwd`` as another alias for the agent identified by ``agent_cwd``.

    Idempotent when ``new_cwd`` already maps to the same agent. Raises if the
    source path is unregistered, or if ``new_cwd`` already belongs to a
    *different* agent (which would silently merge two identities).

    The source ``agent_cwd`` is canonicalized in *lookup* mode
    (``require_exists=False``): it is resolved against an existing
    ``agent_aliases`` row, not used to mint identity, so a moved/renamed repo
    whose old path no longer exists on disk can still be used as the source to
    attach its new path. ``new_cwd`` is canonicalized strictly — it must be an
    existing directory, since it establishes a new alias.
    """
    canonical_src = canonicalize_agent_cwd(agent_cwd, require_exists=False)
    canonical_new = canonicalize_agent_cwd(new_cwd)

    def _work(c: sqlite3.Connection) -> bool:
        agent_id = require_agent_id(c, canonical_src)

        existing_agent_id = resolve_agent_id(c, canonical_new)
        if existing_agent_id is not None:
            if existing_agent_id == agent_id:
                return False  # already an alias of this agent — no-op
            raise ValueError(
                f"new_cwd {canonical_new!r} already maps to a different agent; "
                f"refusing to merge identities."
            )

        c.execute(
            "INSERT INTO agent_aliases (alias_path, agent_id, created_at) "
            "VALUES (?, ?, ?)",
            (canonical_new, agent_id, now_iso()),
        )
        return True

    added = run_in_write_txn(conn, _work)
    return {
        "added": added,
        "canonical_path": canonical_src,
        "new_canonical_path": canonical_new,
    }


def rename_agent(
    conn: sqlite3.Connection, *, agent_cwd: str, name: str
) -> dict[str, Any]:
    """Explicitly change a registered agent's ``name``.

    This is the *only* path that changes a name after first registration:
    ``register_agent`` treats the stored name as sticky and ignores any name it
    is passed for an already-registered path. Use this when the user asks to
    rename the agent.

    ``agent_cwd`` is canonicalized in *lookup* mode (``require_exists=False``)
    so a moved/renamed repo whose old path no longer exists on disk can still be
    renamed via a stored alias. Raises if the path is unregistered or ``name``
    is blank/too long.
    """
    if not name or not name.strip():
        raise ValueError("name is required to rename an agent.")
    _validate_metadata(name=name, workstream=None, description=None)
    canonical = canonicalize_agent_cwd(agent_cwd, require_exists=False)

    def _work(c: sqlite3.Connection) -> sqlite3.Row:
        agent_id = require_agent_id(c, canonical)
        c.execute(
            "UPDATE agents SET name = ?, updated_at = ? WHERE id = ?",
            (name, now_iso(), agent_id),
        )
        return c.execute(
            "SELECT name, workstream, description, created_at, updated_at "
            "FROM agents WHERE id = ?",
            (agent_id,),
        ).fetchone()

    agent = run_in_write_txn(conn, _work)
    return {
        "renamed": True,
        "name": agent["name"],
        "workstream": agent["workstream"],
        "description": agent["description"],
        "canonical_path": canonical,
        "created_at": agent["created_at"],
        "updated_at": agent["updated_at"],
    }


def _row_to_agent_summary(row: sqlite3.Row, *, full: bool) -> dict[str, Any]:
    """Project an ``agents`` row to a public summary.

    Never exposes the internal ULID (locked identity decision): callers only see
    the public handle (``name``/``workstream``), a representative
    ``canonical_path`` and timestamps. ``description`` is clipped to a preview
    unless ``full`` is set, with ``description_truncated`` flagging the clip.
    """
    description = row["description"]
    truncated = False
    if description is not None and not full:
        description, truncated = truncate(description, _DESCRIPTION_PREVIEW_LEN)
    return {
        "name": row["name"],
        "workstream": row["workstream"],
        "description": description,
        "description_truncated": truncated,
        "canonical_path": row["canonical_path"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_agents(
    conn: sqlite3.Connection,
    *,
    limit: int,
    cursor: str | None = None,
    workstream: str | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Return a keyset-paginated directory of all registered agents.

    Global, read-only discovery: unlike the other agent tools this is *not*
    scoped to a single caller, so it needs no ``agent_cwd`` — it lists every
    registered agent so one agent can find peers to address with ``inbox_send``.
    Each row carries the public handle and a representative ``canonical_path``
    (the agent's earliest-registered alias). The internal ULID is never selected
    or returned, including inside the opaque pagination cursor (hard rule:
    agents never see internal IDs).

    Newest-first by ``(created_at, canonical_path)`` for a stable keyset window:
    ``canonical_path`` is an ``agent_aliases`` primary key, so it is globally
    unique and breaks ``created_at`` ties without exposing the ULID. An optional
    ``workstream`` applies an exact-match filter.
    """
    limit = clamp_limit(limit)
    if workstream is not None and not isinstance(workstream, str):
        raise ValueError("workstream filter must be a string.")
    cursor_key = decode_cursor(cursor) if cursor else None
    if cursor_key is not None and len(cursor_key) != 2:
        raise ValueError("invalid cursor for agent list.")

    def _work(c: sqlite3.Connection) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if workstream is not None:
            clauses.append("workstream = ?")
            params.append(workstream)
        if cursor_key is not None:
            frag, frag_params = keyset_predicate(
                [("created_at", cursor_key[0]), ("canonical_path", cursor_key[1])],
                direction="<",
            )
            clauses.append(frag)
            params.extend(frag_params)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit + 1)
        # The ULID (agents.id) is used only inside the correlated subquery to
        # resolve the representative alias; it is never SELECTed into the outer
        # result, so it cannot reach the projection or the cursor.
        return c.execute(
            "SELECT name, workstream, description, created_at, updated_at, "
            "canonical_path FROM ("
            "  SELECT name, workstream, description, created_at, updated_at, "
            "    (SELECT alias_path FROM agent_aliases "
            "       WHERE agent_id = agents.id "
            "       ORDER BY created_at ASC, alias_path ASC LIMIT 1) "
            "      AS canonical_path "
            "  FROM agents"
            f") {where}"
            "ORDER BY created_at DESC, canonical_path DESC LIMIT ?",
            params,
        ).fetchall()

    rows = run_in_read_txn(conn, _work)
    has_more = len(rows) > limit
    rows = rows[:limit]
    agents = [_row_to_agent_summary(row, full=full) for row in rows]
    next_cursor = (
        encode_cursor([rows[-1]["created_at"], rows[-1]["canonical_path"]])
        if has_more and rows
        else None
    )
    return {"agents": agents, "count": len(agents), "next_cursor": next_cursor}


def register_agent_tools(mcp: FastMCP) -> None:
    """Register the agent-identity tools on the FastMCP server."""

    @mcp.tool()
    def agent_register(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        name: Annotated[
            str | None,
            Field(description="Optional name; only set on first registration."),
        ] = None,
        workstream: Annotated[
            str | None,
            Field(description="Optional workstream/grouping label."),
        ] = None,
        description: Annotated[
            str | None,
            Field(description="Optional short description of this agent's role."),
        ] = None,
    ) -> dict[str, Any]:
        """Register (or refresh) the calling agent for its working directory.

        Call this once at session start. It is idempotent: the first call for a
        given repository root creates the agent; later calls update your
        workstream/description (when supplied) and bump ``updated_at``. Omitted
        optional fields are never cleared.

        Naming: if you omit ``name``, the server defaults it to the working
        directory's own name (e.g. ``NP.CoPilot.AgentMemory``) on first
        registration. Prefer that default — surface it to the user and ask
        whether they want a different name before setting one.

        Your name is **sticky**: once you are registered, ``agent_register``
        will NOT change your name, even if you pass a different one — the value
        is silently ignored. This keeps your identity stable across sessions.
        To actually change your name, the user must ask for it, and you call
        ``agent_rename``.

        Args:
            agent_cwd: Your absolute repository root. Use
                ``git rev-parse --show-toplevel`` for git-backed agents.
            name: Optional human-readable agent name. Only used on FIRST
                registration (defaults to the working directory's name when
                omitted); ignored on later calls. Use ``agent_rename`` to change
                an existing name.
            workstream: Optional workstream/grouping label.
            description: Optional short description of this agent's role.

        Returns:
            The stored metadata plus ``registered`` ("new" or "existing") and
            the ``canonical_path`` the server resolved.
        """
        with open_connection() as conn:
            return register_agent(
                conn,
                name=name,
                agent_cwd=agent_cwd,
                workstream=workstream,
                description=description,
            )

    @mcp.tool()
    def agent_describe(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
    ) -> dict[str, Any]:
        """Describe the calling agent: metadata plus open-work counts.

        Args:
            agent_cwd: Your absolute repository root (same value you pass to
                ``agent_register``).

        Returns:
            ``{"registered": False, ...}`` if the path is unknown, otherwise the
            agent metadata with ``unread_messages``, ``open_todos`` and
            ``active_blockers`` counts.
        """
        with open_connection() as conn:
            return describe_agent(conn, agent_cwd=agent_cwd)

    @mcp.tool()
    def agent_rename(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        name: Annotated[
            str,
            Field(description="The new human-readable agent name (non-blank)."),
        ],
    ) -> dict[str, Any]:
        """Change your registered name. Only use when the user asks for it.

        ``agent_register`` treats your name as sticky and ignores any name you
        pass once you are registered, so this is the ONLY way to rename an
        already-registered agent. Do not call this on your own initiative —
        rename only in response to an explicit user request.

        Args:
            agent_cwd: Your absolute repository root (same value you pass to
                ``agent_register``). Resolved against stored aliases, so a moved
                repo's old path still works.
            name: The new human-readable agent name. Must be non-blank.

        Returns:
            The updated metadata plus ``renamed: true`` and the resolved
            ``canonical_path``. Raises if the path is unregistered (call
            ``agent_register`` first) or the name is invalid.
        """
        with open_connection() as conn:
            return rename_agent(conn, agent_cwd=agent_cwd, name=name)

    @mcp.tool()
    def agent_add_alias(
        agent_cwd: Annotated[
            str,
            Field(description="Your absolute repository root, exactly as registered."),
        ],
        new_cwd: Annotated[
            str,
            Field(
                description="The additional absolute path to attach to the same agent."
            ),
        ],
    ) -> dict[str, Any]:
        """Add another working-directory alias for an existing agent.

        Use when the same agent works from a second path (e.g. a new git
        work-tree) that does not canonicalize to the existing root, or to
        recover after a repo move/rename: pass the OLD registered path as
        ``agent_cwd`` and the NEW path as ``new_cwd``. Call this BEFORE
        re-registering from the new path, otherwise ``agent_register`` mints a
        separate agent and this tool will refuse to merge the two identities.

        Args:
            agent_cwd: A path already registered to the agent. Need not still
                exist on disk (so a moved repo's old path still works); it is
                resolved against the stored aliases.
            new_cwd: The additional absolute path to attach to the same agent.
                Must be an existing directory.

        Returns:
            ``added`` (False if the alias already existed) and both canonical
            paths.
        """
        with open_connection() as conn:
            return add_alias(conn, agent_cwd=agent_cwd, new_cwd=new_cwd)

    @mcp.tool()
    def agent_list(
        limit: Annotated[
            int,
            Field(description="Max agents to return this page (capped server-side)."),
        ],
        cursor: Annotated[
            str | None,
            Field(description="Opaque token from a previous call's next_cursor."),
        ] = None,
        workstream: Annotated[
            str | None,
            Field(description="Optional exact-match filter on workstream label."),
        ] = None,
        full: Annotated[
            bool,
            Field(description="Return untruncated description when true."),
        ] = False,
    ) -> dict[str, Any]:
        """List registered agents (a global directory for discovery).

        Use this to find peers to coordinate with — e.g. before ``inbox_send``
        when you know a workstream but not the exact agent name. Unlike the other
        agent tools this is NOT scoped to you and takes no ``agent_cwd``; it
        returns every registered agent, newest first. Internal IDs are never
        exposed.

        Results are keyset-paginated: each call returns at most ``limit`` agents
        (server-capped) plus ``next_cursor``; pass it back as ``cursor`` to page.
        ``description`` is truncated to a preview unless ``full=True``
        (``description_truncated`` flags clipped previews).

        Args:
            limit: Max agents to return this page (>= 1; capped server-side).
            cursor: Opaque token from a previous call's ``next_cursor``.
            workstream: Optional exact-match filter on the workstream label.
            full: Return untruncated ``description`` when true.

        Returns:
            ``agents`` (each with ``name``, ``workstream``, ``description``,
            ``description_truncated``, ``canonical_path``, ``created_at`` and
            ``updated_at``), ``count`` and ``next_cursor`` (null when there is no
            further page).
        """
        with open_connection() as conn:
            return list_agents(
                conn,
                limit=limit,
                cursor=cursor,
                workstream=workstream,
                full=full,
            )
