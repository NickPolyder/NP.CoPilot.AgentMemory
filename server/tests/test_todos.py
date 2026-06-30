"""Tests for the Phase 4 todo tools (tools/todos)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.todos import add_todo, list_todos, update_todo


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


class TestAddTodo:
    def test_creates_pending_todo(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        res = add_todo(db_conn, agent_cwd=agent_cwd, title="ship phase 4")
        assert res["id"]
        assert res["status"] == "pending"
        assert res["priority"] == "normal"

    def test_rejects_blank_title(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="title"):
            add_todo(db_conn, agent_cwd=agent_cwd, title="  ")

    def test_rejects_bad_priority(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="priority"):
            add_todo(db_conn, agent_cwd=agent_cwd, title="x", priority="huge")

    def test_unregistered_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        other = tmp_path / "unreg"
        other.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            add_todo(db_conn, agent_cwd=str(other), title="x")


class TestListTodos:
    def test_recent_sort_newest_first(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        for i in range(3):
            add_todo(db_conn, agent_cwd=agent_cwd, title=f"t{i}")
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [t["title"] for t in out["todos"]] == ["t2", "t1", "t0"]

    def test_priority_sort_urgent_first(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        add_todo(db_conn, agent_cwd=agent_cwd, title="low", priority="low")
        add_todo(db_conn, agent_cwd=agent_cwd, title="urgent", priority="urgent")
        add_todo(db_conn, agent_cwd=agent_cwd, title="normal", priority="normal")
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=10, sort="priority")
        assert [t["title"] for t in out["todos"]] == ["urgent", "normal", "low"]

    def test_status_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        a = add_todo(db_conn, agent_cwd=agent_cwd, title="a")
        add_todo(db_conn, agent_cwd=agent_cwd, title="b")
        update_todo(db_conn, agent_cwd=agent_cwd, todo_id=a["id"], status="in_progress")
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=10, status="in_progress")
        assert out["count"] == 1
        assert out["todos"][0]["id"] == a["id"]

    def test_priority_filter(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        add_todo(db_conn, agent_cwd=agent_cwd, title="hi", priority="high")
        add_todo(db_conn, agent_cwd=agent_cwd, title="lo", priority="low")
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=10, priority="high")
        assert out["count"] == 1
        assert out["todos"][0]["title"] == "hi"

    def test_recent_pagination_no_overlap(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        for i in range(5):
            add_todo(db_conn, agent_cwd=agent_cwd, title=f"t{i}")
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            out = list_todos(db_conn, agent_cwd=agent_cwd, limit=2, cursor=cursor)
            seen.extend(t["id"] for t in out["todos"])
            cursor = out["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 5
        assert len(set(seen)) == 5

    def test_priority_pagination_no_overlap(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        prios = ["low", "urgent", "normal", "high", "urgent", "low"]
        for i, p in enumerate(prios):
            add_todo(db_conn, agent_cwd=agent_cwd, title=f"t{i}", priority=p)
        seen: list[str] = []
        cursor = None
        for _ in range(20):
            out = list_todos(
                db_conn, agent_cwd=agent_cwd, limit=2, sort="priority", cursor=cursor
            )
            seen.extend(t["id"] for t in out["todos"])
            cursor = out["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(prios)
        assert len(set(seen)) == len(prios)

    def test_cursor_sort_mismatch_rejected(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        for i in range(3):
            add_todo(db_conn, agent_cwd=agent_cwd, title=f"t{i}")
        recent = list_todos(db_conn, agent_cwd=agent_cwd, limit=1)
        with pytest.raises(ValueError, match="sort"):
            list_todos(
                db_conn,
                agent_cwd=agent_cwd,
                limit=1,
                sort="priority",
                cursor=recent["next_cursor"],
            )

    def test_description_truncation(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        add_todo(db_conn, agent_cwd=agent_cwd, title="x", description="d" * 1_000)
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=1)
        assert out["todos"][0]["description_truncated"] is True
        full = list_todos(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert len(full["todos"][0]["description"]) == 1_000

    def test_cross_agent_isolation(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        add_todo(db_conn, agent_cwd=agent_cwd, title="mine")
        add_todo(db_conn, agent_cwd=str(other), title="theirs")
        out = list_todos(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [t["title"] for t in out["todos"]] == ["mine"]


class TestUpdateTodo:
    def test_done_stamps_completed_at(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        out = update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], status="done")
        assert out["status"] == "done"
        assert out["completed_at"] is not None

    def test_reopening_clears_completed_at(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], status="done")
        out = update_todo(
            db_conn, agent_cwd=agent_cwd, todo_id=t["id"], status="in_progress"
        )
        assert out["status"] == "in_progress"
        assert out["completed_at"] is None

    def test_updates_multiple_fields(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        out = update_todo(
            db_conn,
            agent_cwd=agent_cwd,
            todo_id=t["id"],
            priority="urgent",
            due_date="2026-12-31",
            description="do it",
        )
        assert out["priority"] == "urgent"
        assert out["due_date"] == "2026-12-31"
        assert out["description"] == "do it"

    def test_requires_at_least_one_field(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        with pytest.raises(ValueError, match="at least one"):
            update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"])

    def test_unknown_todo_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="not found"):
            update_todo(db_conn, agent_cwd=agent_cwd, todo_id="nope", status="done")

    def test_cannot_update_other_agents_todo(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        theirs = add_todo(db_conn, agent_cwd=str(other), title="theirs")
        with pytest.raises(ValueError, match="not found"):
            update_todo(
                db_conn,
                agent_cwd=agent_cwd,
                todo_id=theirs["id"],
                status="done",
            )

    def test_rejects_bad_status(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        with pytest.raises(ValueError, match="status"):
            update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], status="bogus")

    def test_updates_title(self, db_conn: sqlite3.Connection, agent_cwd: str) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="old title")
        out = update_todo(
            db_conn, agent_cwd=agent_cwd, todo_id=t["id"], title="new title"
        )
        assert out["title"] == "new title"

    def test_title_alone_satisfies_at_least_one_field(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        out = update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], title="y")
        assert out["title"] == "y"

    def test_rejects_blank_title(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        with pytest.raises(ValueError, match="title"):
            update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], title="   ")

    def test_rejects_too_long_title(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        t = add_todo(db_conn, agent_cwd=agent_cwd, title="x")
        with pytest.raises(ValueError, match="title is too long"):
            update_todo(db_conn, agent_cwd=agent_cwd, todo_id=t["id"], title="a" * 257)
