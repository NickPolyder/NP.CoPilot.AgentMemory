"""Tests for the Phase 4 memory tools (tools/memory)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.memory import export_memory, log_memory, query_memory


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


@pytest.fixture
def agent_cwd(tmp_path: Path, db_conn: sqlite3.Connection) -> str:
    """A registered agent rooted at a real temp directory."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    register_agent(db_conn, name="tester", agent_cwd=str(cwd))
    return str(cwd)


class TestLogMemory:
    def test_logs_and_returns_id(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        res = log_memory(
            db_conn,
            agent_cwd=agent_cwd,
            category="progress",
            content="did a thing",
            topic="phase-4",
        )
        assert res["id"]
        assert res["category"] == "progress"
        assert res["topic"] == "phase-4"

    def test_persists_metadata_as_object(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        log_memory(
            db_conn,
            agent_cwd=agent_cwd,
            category="note",
            content="x",
            metadata={"k": "v", "n": 1},
        )
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert out["notes"][0]["metadata"] == {"k": "v", "n": 1}

    def test_rejects_bad_category(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="category"):
            log_memory(db_conn, agent_cwd=agent_cwd, category="bogus", content="x")

    def test_rejects_blank_content(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="content"):
            log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="   ")

    def test_rejects_non_object_metadata(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="metadata"):
            log_memory(
                db_conn,
                agent_cwd=agent_cwd,
                category="note",
                content="x",
                metadata=[1, 2, 3],  # type: ignore[arg-type]
            )

    def test_unregistered_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        other = tmp_path / "unreg"
        other.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            log_memory(db_conn, agent_cwd=str(other), category="note", content="x")


class TestQueryMemory:
    def _seed(self, conn: sqlite3.Connection, cwd: str, n: int) -> None:
        for i in range(n):
            log_memory(
                conn,
                agent_cwd=cwd,
                category="progress" if i % 2 else "note",
                content=f"content-{i}",
                topic="t" if i % 2 else None,
            )

    def test_returns_newest_first(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed(db_conn, agent_cwd, 3)
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        contents = [n["content"] for n in out["notes"]]
        assert contents == ["content-2", "content-1", "content-0"]
        assert out["next_cursor"] is None

    def test_category_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        self._seed(db_conn, agent_cwd, 4)
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=10, category="progress")
        assert all(n["category"] == "progress" for n in out["notes"])
        assert out["count"] == 2

    def test_topic_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        self._seed(db_conn, agent_cwd, 4)
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=10, topic="t")
        assert out["count"] == 2
        assert all(n["topic"] == "t" for n in out["notes"])

    def test_pagination_walks_all_without_overlap(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed(db_conn, agent_cwd, 5)
        seen: list[str] = []
        cursor = None
        for _ in range(10):  # safety bound
            out = query_memory(db_conn, agent_cwd=agent_cwd, limit=2, cursor=cursor)
            seen.extend(n["id"] for n in out["notes"])
            cursor = out["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 5
        assert len(set(seen)) == 5  # no duplicates across pages

    def test_truncation_default_and_full(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        big = "x" * 5_000
        log_memory(db_conn, agent_cwd=agent_cwd, category="note", content=big)
        truncated = query_memory(db_conn, agent_cwd=agent_cwd, limit=1)
        note = truncated["notes"][0]
        assert note["content_truncated"] is True
        assert note["content_length"] == 5_000
        assert len(note["content"]) < 5_000

        full = query_memory(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert full["notes"][0]["content"] == big
        assert "content_truncated" not in full["notes"][0]

    def test_since_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        r1 = log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="old")
        r2 = log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="new")
        out = query_memory(
            db_conn, agent_cwd=agent_cwd, limit=10, since=r2["timestamp"]
        )
        ids = [n["id"] for n in out["notes"]]
        assert r2["id"] in ids
        assert r1["id"] not in ids

    def test_cross_agent_isolation(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="mine")
        log_memory(db_conn, agent_cwd=str(other), category="note", content="theirs")
        mine = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [n["content"] for n in mine["notes"]] == ["mine"]


class TestExportMemory:
    def test_renders_markdown(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        log_memory(
            db_conn,
            agent_cwd=agent_cwd,
            category="decision",
            content="chose X",
            topic="arch",
        )
        out = export_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert "# Agent memory" in out["markdown"]
        assert "`decision`" in out["markdown"]
        assert "chose X" in out["markdown"]
        assert out["count"] == 1

    def test_empty_renders_placeholder(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        out = export_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert "_No notes._" in out["markdown"]
        assert out["count"] == 0

    def test_export_is_full_content(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        big = "y" * 4_000
        log_memory(db_conn, agent_cwd=agent_cwd, category="note", content=big)
        out = export_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert big in out["markdown"]
