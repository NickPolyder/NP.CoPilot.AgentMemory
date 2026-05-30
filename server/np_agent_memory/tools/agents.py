"""Agent-identity tools: register, describe, and alias agents.

Agents never see internal ULIDs. They re-identify on every call by passing
their canonical working directory (``agent_cwd``); the server resolves that to
an internal agent row via ``agent_aliases``. See ``np_agent_memory.identity``
for the canonicalization contract and ``docs/spike-roots.md`` for why
``agent_cwd`` is required rather than derived.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory.db import open_connection, run_in_read_txn, run_in_write_txn
from np_agent_memory.identity import canonicalize_agent_cwd, new_ulid, now_iso

# Todo statuses that count as "still open" for the describe summary.
_OPEN_TODO_STATUSES = ("pending", "in_progress", "blocked")
# Blocker statuses that count as "still active" for the describe summary.
_ACTIVE_BLOCKER_STATUSES = ("active", "escalated")


def register_agent(
    conn: sqlite3.Connection,
    *,
    name: str,
    agent_cwd: str,
    workstream: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Idempotently upsert an agent and its alias for ``agent_cwd``.

    First call for a canonical path mints a new ULID + agent + alias. Repeat
    calls update the agent's ``name`` (always) plus ``workstream`` /
    ``description`` *only when provided* (``None`` never erases a stored value)
    and bump ``updated_at``. Runs as a single ``BEGIN IMMEDIATE`` transaction,
    retried on lock contention so a concurrent first-registration race resolves
    to one agent.
    """
    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
        existing = c.execute(
            "SELECT agent_id FROM agent_aliases WHERE alias_path = ?",
            (canonical,),
        ).fetchone()
        ts = now_iso()

        if existing is not None:
            agent_id = existing["agent_id"]
            sets = ["name = ?", "updated_at = ?"]
            params: list[Any] = [name, ts]
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
            agent_id = new_ulid()
            c.execute(
                "INSERT INTO agents "
                "(id, name, workstream, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, name, workstream, description, ts, ts),
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
    three counts share one consistent snapshot. Unknown paths return a soft
    ``{"registered": False}`` (no exception) so a session-start probe can
    branch on it.
    """
    canonical = canonicalize_agent_cwd(agent_cwd)

    def _work(c: sqlite3.Connection) -> dict[str, Any] | None:
        row = c.execute(
            "SELECT agent_id FROM agent_aliases WHERE alias_path = ?",
            (canonical,),
        ).fetchone()
        if row is None:
            return None
        agent_id = row["agent_id"]

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
    """
    canonical_src = canonicalize_agent_cwd(agent_cwd)
    canonical_new = canonicalize_agent_cwd(new_cwd)

    def _work(c: sqlite3.Connection) -> bool:
        src = c.execute(
            "SELECT agent_id FROM agent_aliases WHERE alias_path = ?",
            (canonical_src,),
        ).fetchone()
        if src is None:
            raise ValueError(
                f"agent_cwd is not registered: {canonical_src!r}. "
                f"Call agent_register first."
            )
        agent_id = src["agent_id"]

        existing = c.execute(
            "SELECT agent_id FROM agent_aliases WHERE alias_path = ?",
            (canonical_new,),
        ).fetchone()
        if existing is not None:
            if existing["agent_id"] == agent_id:
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


def register_agent_tools(mcp: FastMCP) -> None:
    """Register the agent-identity tools on the FastMCP server."""

    @mcp.tool()
    def agent_register(
        name: str,
        agent_cwd: str,
        workstream: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Register (or refresh) the calling agent for its working directory.

        Call this once at session start. It is idempotent: the first call for a
        given repository root creates the agent; later calls update your name
        and (when supplied) workstream/description. Omitted optional fields are
        never cleared.

        Args:
            name: Human-readable agent name (e.g. "backend-developer").
            agent_cwd: Your absolute repository root. Use
                ``git rev-parse --show-toplevel`` for git-backed agents.
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
    def agent_describe(agent_cwd: str) -> dict[str, Any]:
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
    def agent_add_alias(agent_cwd: str, new_cwd: str) -> dict[str, Any]:
        """Add another working-directory alias for an existing agent.

        Use when the same agent works from a second path (e.g. a new git
        work-tree) that does not canonicalize to the existing root.

        Args:
            agent_cwd: An absolute path already registered to the agent.
            new_cwd: The additional absolute path to attach to the same agent.

        Returns:
            ``added`` (False if the alias already existed) and both canonical
            paths.
        """
        with open_connection() as conn:
            return add_alias(conn, agent_cwd=agent_cwd, new_cwd=new_cwd)
