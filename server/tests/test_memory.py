"""Tests for the Phase 4 memory tools (tools/memory)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.memory import (
    delete_notes,
    export_memory,
    log_memory,
    query_memory,
    restore_notes,
)


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

    def test_cursor_pages_through_all_notes(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Regression for review R4: export must accept the cursor it returns so
        callers can fetch every page, not just the first."""
        for i in range(5):
            log_memory(db_conn, agent_cwd=agent_cwd, category="note", content=f"c-{i}")

        rendered: list[str] = []
        cursor = None
        for _ in range(10):  # safety bound
            out = export_memory(db_conn, agent_cwd=agent_cwd, limit=2, cursor=cursor)
            rendered.append(out["markdown"])
            cursor = out["next_cursor"]
            if cursor is None:
                break

        joined = "\n".join(rendered)
        for i in range(5):
            assert f"c-{i}" in joined


class TestDeleteNotes:
    def _two_notes(self, conn: sqlite3.Connection, cwd: str) -> tuple[str, str]:
        a = log_memory(conn, agent_cwd=cwd, category="note", content="one")["id"]
        b = log_memory(conn, agent_cwd=cwd, category="decision", content="two")["id"]
        return a, b

    def test_soft_delete_hides_from_query_but_keeps_row(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a, b = self._two_notes(db_conn, agent_cwd)
        res = delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        assert res["mode"] == "soft"
        assert res["deleted"] == 1
        assert res["deleted_ids"] == [a]

        visible = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert [n["id"] for n in visible["notes"]] == [b]

    def test_include_deleted_surfaces_soft_deleted(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a, b = self._two_notes(db_conn, agent_cwd)
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=10, include_deleted=True)
        assert {n["id"] for n in out["notes"]} == {a, b}

    def test_soft_delete_is_default(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Omitting ``hard`` must NOT destroy the row — the safe default."""
        a, _ = self._two_notes(db_conn, agent_cwd)
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        still_there = query_memory(
            db_conn, agent_cwd=agent_cwd, limit=10, include_deleted=True
        )
        assert a in {n["id"] for n in still_there["notes"]}

    def test_soft_delete_idempotent_reports_skipped(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a, _ = self._two_notes(db_conn, agent_cwd)
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        res = delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        assert res["deleted"] == 0
        assert res["skipped"] == [a]

    def test_hard_delete_removes_row(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a, b = self._two_notes(db_conn, agent_cwd)
        res = delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a], hard=True)
        assert res["mode"] == "hard"
        assert res["deleted"] == 1
        gone = query_memory(
            db_conn, agent_cwd=agent_cwd, limit=10, include_deleted=True
        )
        assert {n["id"] for n in gone["notes"]} == {b}

    def test_cannot_delete_another_agents_note(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        theirs = log_memory(
            db_conn, agent_cwd=str(other), category="note", content="theirs"
        )["id"]
        res = delete_notes(db_conn, agent_cwd=agent_cwd, ids=[theirs])
        assert res["deleted"] == 0
        assert res["not_found"] == [theirs]
        # Their note is untouched.
        survives = query_memory(db_conn, agent_cwd=str(other), limit=10)
        assert [n["id"] for n in survives["notes"]] == [theirs]

    def test_empty_ids_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            delete_notes(db_conn, agent_cwd=agent_cwd, ids=[])

    def test_non_string_ids_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="only strings"):
            delete_notes(db_conn, agent_cwd=agent_cwd, ids=[123])  # type: ignore[list-item]

    def test_too_many_ids_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="at most"):
            delete_notes(db_conn, agent_cwd=agent_cwd, ids=["x"] * 201)

    def test_non_bool_hard_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """A truthy non-bool (e.g. the string "false") must NOT take the
        irreversible hard path — it is rejected outright."""
        a, _ = self._two_notes(db_conn, agent_cwd)
        with pytest.raises(ValueError, match="hard must be a boolean"):
            delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a], hard="false")  # type: ignore[arg-type]
        # The note was not touched.
        assert query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["count"] == 2

    def test_query_exposes_deleted_at_marker(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a, _ = self._two_notes(db_conn, agent_cwd)
        live = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["notes"]
        assert all("deleted_at" in n for n in live)
        assert all(n["deleted_at"] is None for n in live)

        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        with_deleted = query_memory(
            db_conn, agent_cwd=agent_cwd, limit=10, include_deleted=True
        )["notes"]
        marked = {n["id"]: n["deleted_at"] for n in with_deleted}
        assert marked[a] is not None  # soft-deleted carries a timestamp


class TestRestoreNotes:
    def _soft_deleted_note(self, conn: sqlite3.Connection, cwd: str) -> str:
        note_id = log_memory(conn, agent_cwd=cwd, category="note", content="x")["id"]
        delete_notes(conn, agent_cwd=cwd, ids=[note_id])
        return note_id

    def test_restore_brings_note_back(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        note_id = self._soft_deleted_note(db_conn, agent_cwd)
        assert query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["count"] == 0

        res = restore_notes(db_conn, agent_cwd=agent_cwd, ids=[note_id])
        assert res["restored"] == 1
        assert res["restored_ids"] == [note_id]

        back = query_memory(db_conn, agent_cwd=agent_cwd, limit=10)["notes"]
        assert [n["id"] for n in back] == [note_id]
        assert back[0]["deleted_at"] is None

    def test_restore_already_live_is_skipped(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        note_id = log_memory(
            db_conn, agent_cwd=agent_cwd, category="note", content="live"
        )["id"]
        res = restore_notes(db_conn, agent_cwd=agent_cwd, ids=[note_id])
        assert res["restored"] == 0
        assert res["skipped"] == [note_id]

    def test_cannot_restore_after_hard_delete(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        note_id = log_memory(
            db_conn, agent_cwd=agent_cwd, category="note", content="gone"
        )["id"]
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[note_id], hard=True)
        res = restore_notes(db_conn, agent_cwd=agent_cwd, ids=[note_id])
        assert res["restored"] == 0
        assert res["not_found"] == [note_id]

    def test_cannot_restore_another_agents_note(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        theirs = self._soft_deleted_note(db_conn, str(other))
        res = restore_notes(db_conn, agent_cwd=agent_cwd, ids=[theirs])
        assert res["restored"] == 0
        assert res["not_found"] == [theirs]
        # Still soft-deleted for the owner (not resurrected by the other agent).
        assert query_memory(db_conn, agent_cwd=str(other), limit=10)["count"] == 0

    def test_empty_ids_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="non-empty list"):
            restore_notes(db_conn, agent_cwd=agent_cwd, ids=[])


class TestMemoryExportDeleted:
    def test_export_hides_soft_deleted_by_default(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a = log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="hideme")[
            "id"
        ]
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        out = export_memory(db_conn, agent_cwd=agent_cwd, limit=10)
        assert "hideme" not in out["markdown"]
        assert out["count"] == 0

    def test_export_include_deleted_shows_marker(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        a = log_memory(db_conn, agent_cwd=agent_cwd, category="note", content="showme")[
            "id"
        ]
        delete_notes(db_conn, agent_cwd=agent_cwd, ids=[a])
        out = export_memory(
            db_conn, agent_cwd=agent_cwd, limit=10, include_deleted=True
        )
        assert "showme" in out["markdown"]
        assert "_(deleted)_" in out["markdown"]
        assert out["count"] == 1
