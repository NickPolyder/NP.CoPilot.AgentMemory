"""Tests for the Phase 3 agent-identity layer (identity + tools/agents)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import init_db, open_connection
from np_agent_memory.identity import canonicalize_agent_cwd, new_ulid, now_iso
from np_agent_memory.tools import register_all_tools
from np_agent_memory.tools.agents import add_alias, describe_agent, register_agent


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Yield a connection to a freshly migrated temp database."""
    data_dir = tmp_path / "data"
    db_path = init_db(data_dir)
    with open_connection(db_path) as conn:
        yield conn


def _agent_id_for(conn: sqlite3.Connection, canonical: str) -> str:
    row = conn.execute(
        "SELECT agent_id FROM agent_aliases WHERE alias_path = ?", (canonical,)
    ).fetchone()
    assert row is not None
    return row["agent_id"]


# ---------------------------------------------------------------------------
# canonicalize_agent_cwd
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            canonicalize_agent_cwd("   ")

    def test_rejects_relative(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            canonicalize_agent_cwd("relative/path")

    def test_rejects_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        with pytest.raises(ValueError, match="does not exist"):
            canonicalize_agent_cwd(str(missing))

    def test_rejects_file(self, tmp_path: Path) -> None:
        f = tmp_path / "afile.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="must be a directory"):
            canonicalize_agent_cwd(str(f))

    def test_uses_forward_slashes(self, tmp_path: Path) -> None:
        assert "\\" not in canonicalize_agent_cwd(str(tmp_path))

    def test_trailing_separator_is_idempotent(self, tmp_path: Path) -> None:
        base = canonicalize_agent_cwd(str(tmp_path))
        with_sep = canonicalize_agent_cwd(str(tmp_path) + "\\")
        assert base == with_sep
        assert not base.endswith("/")

    def test_case_insensitive_on_windows(self, tmp_path: Path) -> None:
        # Windows filesystems are case-insensitive; normcase folds case.
        lower = canonicalize_agent_cwd(str(tmp_path).lower())
        upper = canonicalize_agent_cwd(str(tmp_path).upper())
        assert lower == upper

    def test_anchor_keeps_its_slash(self) -> None:
        # Drive root must not be stripped to a bare drive letter.
        anchor = Path.cwd().anchor  # e.g. "C:\\"
        canon = canonicalize_agent_cwd(anchor)
        assert canon.endswith("/")
        assert canon.endswith(":/")


class TestUlidAndTimestamp:
    def test_new_ulid_unique_and_sized(self) -> None:
        a, b = new_ulid(), new_ulid()
        assert a != b
        assert len(a) == 26

    def test_now_iso_has_offset(self) -> None:
        assert "+00:00" in now_iso()


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------


class TestRegisterAgent:
    def test_first_registration_is_new(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = register_agent(
            db_conn, name="backend", agent_cwd=str(tmp_path), workstream="np"
        )
        assert result["registered"] == "new"
        assert result["name"] == "backend"
        assert result["workstream"] == "np"
        assert "\\" not in result["canonical_path"]

    def test_no_internal_id_leaked(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = register_agent(db_conn, name="backend", agent_cwd=str(tmp_path))
        assert "id" not in result
        assert "agent_id" not in result

    def test_repeat_registration_is_existing_and_updates_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="old", agent_cwd=str(tmp_path))
        result = register_agent(db_conn, name="new-name", agent_cwd=str(tmp_path))
        assert result["registered"] == "existing"
        assert result["name"] == "new-name"

    def test_omitted_workstream_does_not_erase(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path), workstream="np")
        result = register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        assert result["workstream"] == "np"

    def test_description_is_settable_and_preserved(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(
            db_conn, name="a", agent_cwd=str(tmp_path), description="does things"
        )
        result = register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        assert result["description"] == "does things"

    def test_one_agent_per_canonical_path(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path) + "\\")
        count = db_conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# describe_agent
# ---------------------------------------------------------------------------


class TestDescribeAgent:
    def test_unregistered_is_soft_false(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["registered"] is False
        assert "hint" in result

    def test_registered_returns_zero_counts(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["registered"] is True
        assert result["unread_messages"] == 0
        assert result["open_todos"] == 0
        assert result["active_blockers"] == 0

    def test_counts_reflect_open_work(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        canonical = canonicalize_agent_cwd(str(tmp_path))
        agent_id = _agent_id_for(db_conn, canonical)
        ts = now_iso()

        # Open todo (counted) + done todo (not counted).
        db_conn.execute(
            "INSERT INTO todos (id, agent_id, title, status, priority, "
            "created_at, updated_at) VALUES (?, ?, 'open', 'pending', 'normal', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )
        db_conn.execute(
            "INSERT INTO todos (id, agent_id, title, status, priority, "
            "created_at, updated_at) VALUES (?, ?, 'closed', 'done', 'normal', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )
        # Active blocker (counted) + resolved blocker (not counted).
        db_conn.execute(
            "INSERT INTO blockers (id, agent_id, title, status, raised_at) "
            "VALUES (?, ?, 'b1', 'active', ?)",
            (new_ulid(), agent_id, ts),
        )
        db_conn.execute(
            "INSERT INTO blockers (id, agent_id, title, status, raised_at) "
            "VALUES (?, ?, 'b2', 'resolved', ?)",
            (new_ulid(), agent_id, ts),
        )
        # Unread inbox message (counted) + read message (not counted).
        db_conn.execute(
            "INSERT INTO inbox (id, to_agent_id, subject, body, sent_at) "
            "VALUES (?, ?, 's', 'b', ?)",
            (new_ulid(), agent_id, ts),
        )
        db_conn.execute(
            "INSERT INTO inbox (id, to_agent_id, subject, body, sent_at, read_at) "
            "VALUES (?, ?, 's', 'b', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )

        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["open_todos"] == 1
        assert result["active_blockers"] == 1
        assert result["unread_messages"] == 1


# ---------------------------------------------------------------------------
# add_alias
# ---------------------------------------------------------------------------


class TestAddAlias:
    def test_adds_and_resolves_to_same_agent(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        primary = tmp_path / "primary"
        secondary = tmp_path / "secondary"
        primary.mkdir()
        secondary.mkdir()

        register_agent(db_conn, name="a", agent_cwd=str(primary))
        result = add_alias(db_conn, agent_cwd=str(primary), new_cwd=str(secondary))
        assert result["added"] is True

        via_alias = describe_agent(db_conn, agent_cwd=str(secondary))
        assert via_alias["registered"] is True
        assert via_alias["name"] == "a"

    def test_idempotent_no_op(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        secondary = tmp_path / "secondary"
        secondary.mkdir()
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))
        again = add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))
        assert again["added"] is False

    def test_unregistered_source_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        secondary = tmp_path / "secondary"
        secondary.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))

    def test_conflict_with_different_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_a.mkdir()
        path_b.mkdir()
        register_agent(db_conn, name="agent-a", agent_cwd=str(path_a))
        register_agent(db_conn, name="agent-b", agent_cwd=str(path_b))
        with pytest.raises(ValueError, match="different agent"):
            add_alias(db_conn, agent_cwd=str(path_a), new_cwd=str(path_b))


# ---------------------------------------------------------------------------
# tool registration wiring
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_registers_expected_tools(self) -> None:
        from mcp.server.fastmcp import FastMCP

        probe = FastMCP(name="probe")
        register_all_tools(probe)
        import anyio

        tools = anyio.run(probe.list_tools)
        names = {t.name for t in tools}
        assert {"agent_register", "agent_describe", "agent_add_alias"} <= names
