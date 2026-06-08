"""Tests for the Phase 5 handover tools (tools/handovers)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.handovers import (
    ack_handovers,
    claim_handovers,
    export_handover,
    latest_handover,
    release_handovers,
    save_handover,
)


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


class TestSaveHandover:
    def test_saves_and_returns_id(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        res = save_handover(
            db_conn, agent_cwd=agent_cwd, summary="done phase 5", body_md="# body"
        )
        assert res["id"]
        assert res["saved_at"]

    def test_rejects_blank_summary(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="summary"):
            save_handover(db_conn, agent_cwd=agent_cwd, summary=" ", body_md="x")

    def test_rejects_blank_body(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="body_md"):
            save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md="  ")

    def test_unregistered_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        other = tmp_path / "unreg"
        other.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            save_handover(db_conn, agent_cwd=str(other), summary="s", body_md="b")


class TestLatestAndExport:
    def test_latest_returns_most_recent(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        save_handover(db_conn, agent_cwd=agent_cwd, summary="old", body_md="b1")
        save_handover(db_conn, agent_cwd=agent_cwd, summary="new", body_md="b2")
        out = latest_handover(db_conn, agent_cwd=agent_cwd)
        assert out["handover"]["summary"] == "new"

    def test_latest_none_when_empty(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        out = latest_handover(db_conn, agent_cwd=agent_cwd)
        assert out["handover"] is None

    def test_latest_truncates_body(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        big = "x" * 5_000
        save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md=big)
        truncated = latest_handover(db_conn, agent_cwd=agent_cwd)
        assert truncated["handover"]["body_truncated"] is True
        full = latest_handover(db_conn, agent_cwd=agent_cwd, full=True)
        assert full["handover"]["body_md"] == big

    def test_export_renders_markdown(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        save_handover(
            db_conn, agent_cwd=agent_cwd, summary="ship it", body_md="## details"
        )
        out = export_handover(db_conn, agent_cwd=agent_cwd)
        assert "# Handover" in out["markdown"]
        assert "ship it" in out["markdown"]
        assert "## details" in out["markdown"]

    def test_export_specific_id(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        first = save_handover(
            db_conn, agent_cwd=agent_cwd, summary="first", body_md="b1"
        )
        save_handover(db_conn, agent_cwd=agent_cwd, summary="second", body_md="b2")
        out = export_handover(db_conn, agent_cwd=agent_cwd, handover_id=first["id"])
        assert "first" in out["markdown"]

    def test_export_missing_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="no handover"):
            export_handover(db_conn, agent_cwd=agent_cwd, handover_id="nope")

    def test_latest_is_per_agent(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        save_handover(db_conn, agent_cwd=agent_cwd, summary="mine", body_md="b")
        save_handover(db_conn, agent_cwd=str(other), summary="theirs", body_md="b")
        assert (
            latest_handover(db_conn, agent_cwd=agent_cwd)["handover"]["summary"]
            == "mine"
        )


class TestClaimAckRelease:
    def _seed(self, conn: sqlite3.Connection, cwd: str, n: int) -> list[str]:
        return [
            save_handover(conn, agent_cwd=cwd, summary=f"s{i}", body_md=f"b{i}")["id"]
            for i in range(n)
        ]

    def test_claim_returns_oldest_first_with_agent_name(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed(db_conn, agent_cwd, 3)
        out = claim_handovers(db_conn, consumer_id="ingest", limit=10)
        assert out["count"] == 3
        assert [h["summary"] for h in out["handovers"]] == ["s0", "s1", "s2"]
        assert out["handovers"][0]["agent_name"] == "tester"
        assert out["handovers"][0]["attempt_count"] == 1

    def test_claim_excludes_already_claimed(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed(db_conn, agent_cwd, 3)
        first = claim_handovers(db_conn, consumer_id="ingest", limit=2)
        assert first["count"] == 2
        second = claim_handovers(db_conn, consumer_id="ingest", limit=10)
        assert second["count"] == 1  # the third, fresh one

    def test_stale_claim_is_reclaimable(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed(db_conn, agent_cwd, 1)
        claim_handovers(db_conn, consumer_id="ingest-a", limit=10)
        # Not reclaimable yet with a generous staleness window.
        assert claim_handovers(db_conn, consumer_id="ingest-b", limit=10)["count"] == 0
        # Backdate the claim so it is now stale.
        db_conn.execute("update handovers set claimed_at = '2000-01-01T00:00:00+00:00'")
        out = claim_handovers(
            db_conn, consumer_id="ingest-b", limit=10, stale_minutes=15
        )
        assert out["count"] == 1
        assert out["handovers"][0]["attempt_count"] == 2  # incremented twice

    def test_ack_marks_consumed_and_only_by_claimer(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        ids = self._seed(db_conn, agent_cwd, 2)
        claim_handovers(db_conn, consumer_id="ingest", limit=10)
        # A different consumer cannot ack.
        wrong = ack_handovers(db_conn, consumer_id="other", ids=ids)
        assert wrong["acked"] == 0
        assert sorted(wrong["skipped"]) == sorted(ids)
        # The claiming consumer can.
        ok = ack_handovers(db_conn, consumer_id="ingest", ids=ids)
        assert ok["acked"] == 2
        # Acked handovers are no longer claimable.
        assert claim_handovers(db_conn, consumer_id="ingest", limit=10)["count"] == 0

    def test_ack_is_idempotent(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        ids = self._seed(db_conn, agent_cwd, 1)
        claim_handovers(db_conn, consumer_id="ingest", limit=10)
        assert ack_handovers(db_conn, consumer_id="ingest", ids=ids)["acked"] == 1
        # Second ack does nothing (already consumed).
        assert ack_handovers(db_conn, consumer_id="ingest", ids=ids)["acked"] == 0

    def test_release_returns_to_pool_with_error(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        ids = self._seed(db_conn, agent_cwd, 1)
        claim_handovers(db_conn, consumer_id="ingest", limit=10)
        out = release_handovers(
            db_conn, consumer_id="ingest", ids=ids, last_error="db down"
        )
        assert out["released"] == 1
        # Immediately reclaimable by anyone (claim was cleared).
        reclaim = claim_handovers(db_conn, consumer_id="ingest-2", limit=10)
        assert reclaim["count"] == 1
        row = db_conn.execute(
            "select last_error from handovers where id = ?", (ids[0],)
        ).fetchone()
        assert row["last_error"] == "db down"

    def test_release_only_by_claimer(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        ids = self._seed(db_conn, agent_cwd, 1)
        claim_handovers(db_conn, consumer_id="ingest", limit=10)
        out = release_handovers(db_conn, consumer_id="someone-else", ids=ids)
        assert out["released"] == 0

    def test_claim_rejects_blank_consumer(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="consumer_id"):
            claim_handovers(db_conn, consumer_id="  ", limit=10)

    def test_ack_rejects_empty_ids(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="ids"):
            ack_handovers(db_conn, consumer_id="ingest", ids=[])
