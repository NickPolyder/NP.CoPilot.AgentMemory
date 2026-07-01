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
    _BODY_PREVIEW_LEN,
    _MAX_CLAIM_ATTEMPTS,
    _MAX_ERROR_LEN,
    _MAX_SESSION_LEN,
    _MAX_STALE_MINUTES,
    _MAX_SUMMARY_LEN,
    ack_handovers,
    claim_handovers,
    export_handover,
    latest_handover,
    list_quarantined_handovers,
    release_handovers,
    save_handover,
)


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return an initialized DB path (no open connection) for concurrency tests."""
    return init_db(tmp_path / "data")


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


class TestClaimFullBody:
    def _seed_big(self, conn: sqlite3.Connection, cwd: str) -> str:
        return save_handover(conn, agent_cwd=cwd, summary="s", body_md="x" * 5_000)[
            "id"
        ]

    def test_claim_defaults_to_full_body(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed_big(db_conn, agent_cwd)
        out = claim_handovers(db_conn, consumer_id="ingest", limit=10)
        h = out["handovers"][0]
        assert len(h["body_md"]) == 5_000
        assert "body_truncated" not in h

    def test_claim_full_false_truncates_body(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        self._seed_big(db_conn, agent_cwd)
        out = claim_handovers(db_conn, consumer_id="ingest", limit=10, full=False)
        h = out["handovers"][0]
        assert h["body_truncated"] is True
        assert h["body_length"] == 5_000
        assert len(h["body_md"]) < 5_000


class TestQuarantine:
    def _seed(self, conn: sqlite3.Connection, cwd: str, n: int) -> list[str]:
        return [
            save_handover(conn, agent_cwd=cwd, summary=f"s{i}", body_md=f"b{i}")["id"]
            for i in range(n)
        ]

    def _drive_to_cap(self, conn: sqlite3.Connection, consumer: str, hid: str) -> None:
        """Claim once then force attempt_count to the dead-letter cap."""
        claim_handovers(conn, consumer_id=consumer, limit=10)
        conn.execute(
            "update handovers set attempt_count = ? where id = ?",
            (_MAX_CLAIM_ATTEMPTS, hid),
        )

    def test_release_at_cap_quarantines(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        (hid,) = self._seed(db_conn, agent_cwd, 1)
        self._drive_to_cap(db_conn, "ingest", hid)
        out = release_handovers(
            db_conn, consumer_id="ingest", ids=[hid], last_error="poison"
        )
        assert out["quarantined"] == 1
        assert out["quarantined_ids"] == [hid]
        assert out["released"] == 0

    def test_quarantined_is_not_claimable(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        (hid,) = self._seed(db_conn, agent_cwd, 1)
        self._drive_to_cap(db_conn, "ingest", hid)
        release_handovers(db_conn, consumer_id="ingest", ids=[hid])
        assert claim_handovers(db_conn, consumer_id="anyone", limit=10)["count"] == 0

    def test_release_below_cap_does_not_quarantine(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        (hid,) = self._seed(db_conn, agent_cwd, 1)
        claim_handovers(db_conn, consumer_id="ingest", limit=10)  # attempt_count = 1
        out = release_handovers(db_conn, consumer_id="ingest", ids=[hid])
        assert out["released"] == 1
        assert out["quarantined"] == 0

    def test_list_quarantined_surfaces_forensics(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        (hid,) = self._seed(db_conn, agent_cwd, 1)
        self._drive_to_cap(db_conn, "ingest", hid)
        release_handovers(db_conn, consumer_id="ingest", ids=[hid], last_error="boom")
        out = list_quarantined_handovers(db_conn, limit=10)
        assert out["count"] == 1
        row = out["handovers"][0]
        assert row["id"] == hid
        assert row["last_error"] == "boom"
        assert row["quarantined_at"] is not None
        assert row["claimed_by"] == "ingest"
        assert row["agent_name"] == "tester"

    def test_list_quarantined_paginates_newest_first(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        ids = self._seed(db_conn, agent_cwd, 3)
        for hid in ids:
            self._drive_to_cap(db_conn, "ingest", hid)
            release_handovers(db_conn, consumer_id="ingest", ids=[hid])
        seen: list[str] = []
        cursor = None
        for _ in range(3):
            page = list_quarantined_handovers(db_conn, limit=1, cursor=cursor)
            assert page["count"] == 1
            seen.append(page["handovers"][0]["id"])
            cursor = page["next_cursor"]
        assert page["next_cursor"] is None
        assert sorted(seen) == sorted(ids)


# ---------------------------------------------------------------------------
# Gap 1 — Concurrent 2-consumer claim: disjoint sets, no double-claim
# ---------------------------------------------------------------------------


class TestConcurrentClaim:
    def test_two_consumers_claim_disjoint_sets(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """Two consumers on separate connections must never claim the same row."""
        cwd = tmp_path / "repo"
        cwd.mkdir()
        with open_connection(db_path) as setup:
            register_agent(setup, name="tester", agent_cwd=str(cwd))
            for i in range(6):
                save_handover(
                    setup, agent_cwd=str(cwd), summary=f"s{i}", body_md=f"b{i}"
                )

        with open_connection(db_path) as conn_a, open_connection(db_path) as conn_b:
            out_a = claim_handovers(conn_a, consumer_id="consumer-a", limit=4)
            out_b = claim_handovers(conn_b, consumer_id="consumer-b", limit=4)

        ids_a = {h["id"] for h in out_a["handovers"]}
        ids_b = {h["id"] for h in out_b["handovers"]}

        assert ids_a.isdisjoint(ids_b), "Two consumers claimed the same handover"
        assert out_a["count"] + out_b["count"] <= 6

    def test_total_claimed_does_not_exceed_seeded(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """Even when both consumers request more than available, total stays <= N."""
        n = 3
        cwd = tmp_path / "repo"
        cwd.mkdir()
        with open_connection(db_path) as setup:
            register_agent(setup, name="tester", agent_cwd=str(cwd))
            for i in range(n):
                save_handover(
                    setup, agent_cwd=str(cwd), summary=f"s{i}", body_md=f"b{i}"
                )

        with open_connection(db_path) as conn_a, open_connection(db_path) as conn_b:
            out_a = claim_handovers(conn_a, consumer_id="alpha", limit=10)
            out_b = claim_handovers(conn_b, consumer_id="beta", limit=10)

        total = out_a["count"] + out_b["count"]
        assert total == n  # all 3 are claimed, each exactly once
        ids_a = {h["id"] for h in out_a["handovers"]}
        ids_b = {h["id"] for h in out_b["handovers"]}
        assert ids_a.isdisjoint(ids_b)


# ---------------------------------------------------------------------------
# Gap 2 — claim_handovers validation branches + stale_minutes=0
# ---------------------------------------------------------------------------


class TestClaimValidation:
    def test_stale_minutes_zero_makes_just_claimed_reclaimable(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """stale_minutes=0 means every existing claim is immediately stale."""
        save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md="b")
        first = claim_handovers(db_conn, consumer_id="consumer-a", limit=10)
        assert first["count"] == 1
        # With the default stale window the claim is NOT yet reclaimable.
        assert (
            claim_handovers(db_conn, consumer_id="consumer-b", limit=10)["count"] == 0
        )
        # With stale_minutes=0 the just-made claim is immediately stale.
        second = claim_handovers(
            db_conn, consumer_id="consumer-b", limit=10, stale_minutes=0
        )
        assert second["count"] == 1

    def test_non_int_stale_minutes_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="stale_minutes must be an integer"):
            claim_handovers(
                db_conn,
                consumer_id="ingest",
                limit=10,
                stale_minutes="15",  # type: ignore[arg-type]
            )

    def test_float_stale_minutes_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="stale_minutes must be an integer"):
            claim_handovers(
                db_conn,
                consumer_id="ingest",
                limit=10,
                stale_minutes=1.5,  # type: ignore[arg-type]
            )

    def test_bool_stale_minutes_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        # bool is a subclass of int, but the validator explicitly rejects it.
        with pytest.raises(ValueError, match="stale_minutes must be an integer"):
            claim_handovers(
                db_conn,
                consumer_id="ingest",
                limit=10,
                stale_minutes=True,  # type: ignore[arg-type]
            )

    def test_negative_stale_minutes_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="stale_minutes must be >= 0"):
            claim_handovers(db_conn, consumer_id="ingest", limit=10, stale_minutes=-1)

    def test_stale_minutes_above_max_is_capped_not_raised(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """A stale_minutes value above _MAX_STALE_MINUTES is silently capped."""
        save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md="b")
        # Must not raise; returns normally with the capped window.
        out = claim_handovers(
            db_conn,
            consumer_id="ingest",
            limit=10,
            stale_minutes=_MAX_STALE_MINUTES + 100,
        )
        assert "handovers" in out


# ---------------------------------------------------------------------------
# Gap 5 (body) — exact-boundary truncation for body_md
# ---------------------------------------------------------------------------


class TestBodyTruncationBoundary:
    def test_body_at_exactly_preview_len_not_truncated(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Body exactly _BODY_PREVIEW_LEN chars: no truncation flag, full body
        returned.
        """
        body = "x" * _BODY_PREVIEW_LEN
        save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md=body)
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=False)
        h = out["handover"]
        assert "body_truncated" not in h
        assert len(h["body_md"]) == _BODY_PREVIEW_LEN

    def test_body_one_over_preview_len_is_truncated(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Body at _BODY_PREVIEW_LEN + 1 chars: truncation flag set, body clipped."""
        body = "x" * (_BODY_PREVIEW_LEN + 1)
        save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md=body)
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=False)
        h = out["handover"]
        assert h["body_truncated"] is True
        assert h["body_length"] == _BODY_PREVIEW_LEN + 1
        assert len(h["body_md"]) == _BODY_PREVIEW_LEN


# ---------------------------------------------------------------------------
# Gap 6 — caps + metadata round-trip + cross-agent export isolation
# ---------------------------------------------------------------------------


class TestHandoverCapsAndIsolation:
    def test_summary_too_long_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="summary"):
            save_handover(
                db_conn,
                agent_cwd=agent_cwd,
                summary="x" * (_MAX_SUMMARY_LEN + 1),
                body_md="body",
            )

    def test_session_id_too_long_raises(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        with pytest.raises(ValueError, match="session_id"):
            save_handover(
                db_conn,
                agent_cwd=agent_cwd,
                summary="s",
                body_md="b",
                session_id="x" * (_MAX_SESSION_LEN + 1),
            )

    def test_last_error_too_long_raises_on_release(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        hid = save_handover(db_conn, agent_cwd=agent_cwd, summary="s", body_md="b")[
            "id"
        ]
        claim_handovers(db_conn, consumer_id="ingest", limit=10)
        with pytest.raises(ValueError, match="last_error"):
            release_handovers(
                db_conn,
                consumer_id="ingest",
                ids=[hid],
                last_error="x" * (_MAX_ERROR_LEN + 1),
            )

    def test_metadata_round_trips_through_latest(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        meta = {"phase": "5", "count": 42, "flag": True}
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary="meta test",
            body_md="b",
            metadata=meta,
        )
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=True)
        assert out["handover"]["metadata"] == meta

    def test_metadata_round_trips_through_claim(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        meta = {"ingest_hint": "compress", "version": 3}
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary="meta test",
            body_md="b",
            metadata=meta,
        )
        out = claim_handovers(db_conn, consumer_id="ingest", limit=10)
        assert out["handovers"][0]["metadata"] == meta

    def test_export_cross_agent_isolation(
        self, db_conn: sqlite3.Connection, agent_cwd: str, tmp_path: Path
    ) -> None:
        """export_handover must never return a different agent's handovers."""
        other = tmp_path / "other_agent"
        other.mkdir()
        register_agent(db_conn, name="other", agent_cwd=str(other))
        save_handover(db_conn, agent_cwd=agent_cwd, summary="mine", body_md="my body")
        save_handover(
            db_conn, agent_cwd=str(other), summary="theirs", body_md="their body"
        )
        out = export_handover(db_conn, agent_cwd=agent_cwd)
        assert "mine" in out["markdown"]
        assert "theirs" not in out["markdown"]

    def test_null_metadata_is_none_in_latest(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        save_handover(db_conn, agent_cwd=agent_cwd, summary="no meta", body_md="b")
        out = latest_handover(db_conn, agent_cwd=agent_cwd)
        assert out["handover"]["metadata"] is None
