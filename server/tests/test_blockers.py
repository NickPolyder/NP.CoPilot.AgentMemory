"""Tests for the Phase 5 blocker tools (tools/blockers)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.blockers import list_blockers, open_blocker, resolve_blocker
from np_agent_memory.tools.memory import query_memory


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


@pytest.fixture
def agent_cwd(tmp_path: Path, db_conn: sqlite3.Connection) -> str:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    register_agent(db_conn, name="tester", agent_cwd=str(cwd))
    return str(cwd)


class TestOpenBlocker:
    def test_creates_active_blocker(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        res = open_blocker(db_conn, agent_cwd=agent_cwd, title="waiting on infra")
        assert res["id"]
        assert res["status"] == "active"

    def test_auto_logs_related_note(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        res = open_blocker(db_conn, agent_cwd=agent_cwd, title="blocked X")
        notes = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["notes"]
        assert len(notes) == 1
        assert notes[0]["related_type"] == "blocker"
        assert notes[0]["related_id"] == res["id"]
        assert "Blocker opened" in notes[0]["content"]

    def test_rejects_blank_title(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="title"):
            open_blocker(db_conn, agent_cwd=agent_cwd, title="  ")

    def test_duplicate_external_key_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        open_blocker(db_conn, agent_cwd=agent_cwd, title="a", external_key="k1")
        with pytest.raises(ValueError, match="external_key"):
            open_blocker(db_conn, agent_cwd=agent_cwd, title="b", external_key="k1")

    def test_unregistered_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        other = tmp_path / "unreg"
        other.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            open_blocker(db_conn, agent_cwd=str(other), title="x")


class TestListBlockers:
    def test_newest_first(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        for i in range(3):
            open_blocker(db_conn, agent_cwd=agent_cwd, title=f"b{i}")
        out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [b["title"] for b in out["blockers"]] == ["b2", "b1", "b0"]

    def test_status_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        a = open_blocker(db_conn, agent_cwd=agent_cwd, title="a")
        open_blocker(db_conn, agent_cwd=agent_cwd, title="b")
        resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id=a["id"])
        out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=10, status="active")
        assert out["count"] == 1
        assert out["blockers"][0]["title"] == "b"

    def test_workstream_filter(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        open_blocker(db_conn, agent_cwd=agent_cwd, title="a", workstream="bi")
        open_blocker(db_conn, agent_cwd=agent_cwd, title="b", workstream="ops")
        out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=10, workstream="bi")
        assert out["count"] == 1
        assert out["blockers"][0]["title"] == "a"

    def test_pagination_no_overlap(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        for i in range(5):
            open_blocker(db_conn, agent_cwd=agent_cwd, title=f"b{i}")
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=2, cursor=cursor)
            seen.extend(b["id"] for b in out["blockers"])
            cursor = out["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 5
        assert len(set(seen)) == 5

    def test_description_truncation(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        open_blocker(db_conn, agent_cwd=agent_cwd, title="x", description="d" * 1_000)
        out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=1)
        assert out["blockers"][0]["description_truncated"] is True
        full = list_blockers(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert len(full["blockers"][0]["description"]) == 1_000

    def test_cross_agent_isolation(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        open_blocker(db_conn, agent_cwd=agent_cwd, title="mine")
        open_blocker(db_conn, agent_cwd=str(other), title="theirs")
        out = list_blockers(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [b["title"] for b in out["blockers"]] == ["mine"]


class TestResolveBlocker:
    def test_resolves_and_stamps(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        b = open_blocker(db_conn, agent_cwd=agent_cwd, title="x")
        out = resolve_blocker(
            db_conn, agent_cwd=agent_cwd, blocker_id=b["id"], resolution="fixed"
        )
        assert out["status"] == "resolved"
        assert out["resolved_at"] is not None
        assert out["resolution"] == "fixed"

    def test_resolve_auto_logs_note(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        b = open_blocker(db_conn, agent_cwd=agent_cwd, title="x")
        resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id=b["id"])
        notes = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["notes"]
        assert any("Blocker resolved" in n["content"] for n in notes)

    def test_double_resolve_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        b = open_blocker(db_conn, agent_cwd=agent_cwd, title="x")
        resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id=b["id"])
        with pytest.raises(ValueError, match="already resolved"):
            resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id=b["id"])

    def test_unknown_blocker_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id="nope")

    def test_cannot_resolve_other_agents_blocker(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        theirs = open_blocker(db_conn, agent_cwd=str(other), title="theirs")
        with pytest.raises(ValueError, match="not found"):
            resolve_blocker(db_conn, agent_cwd=agent_cwd, blocker_id=theirs["id"])
