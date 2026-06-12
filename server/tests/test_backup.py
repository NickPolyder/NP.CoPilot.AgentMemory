"""Tests for Phase 7 backup machinery."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from np_agent_memory.backup import (
    maybe_daily_backup,
    prune_backups,
    run_backup,
    start_lazy_daily_backup,
)
from np_agent_memory.db import open_connection
from np_agent_memory.identity import new_ulid, now_iso
from np_agent_memory.startup import init_db


def _insert_agent(db_path: Path) -> None:
    ts = now_iso()
    with open_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO agents (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (new_ulid(), "backup-test", ts, ts),
        )


def _agent_count(db_path: Path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0])


def _backup_run_rows(db_path: Path) -> list[sqlite3.Row]:
    with open_connection(db_path) as conn:
        return conn.execute(
            "SELECT started_at, finished_at, path, success FROM backup_runs ORDER BY id"
        ).fetchall()


def _today_backup_path(backups_dir: Path) -> Path:
    return backups_dir / f"agent-memory-{date.today().isoformat()}.db"


class TestRunBackup:
    def test_creates_valid_copy_and_records_success(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        _insert_agent(db_path)

        backup_path = run_backup(db_path, backups_dir)

        assert backup_path == _today_backup_path(backups_dir)
        assert backup_path.exists()
        assert _agent_count(backup_path) == _agent_count(db_path) == 1

        rows = _backup_run_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["success"] == 1
        assert rows[0]["finished_at"] is not None
        assert rows[0]["path"] == str(backup_path)

    def test_failure_records_unsuccessful_run(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        # A directory at the destination makes the atomic rename fail.
        _today_backup_path(backups_dir).mkdir(parents=True)

        with pytest.raises(OSError):
            run_backup(db_path, backups_dir)

        rows = _backup_run_rows(db_path)
        assert len(rows) == 1
        assert rows[0]["success"] == 0
        assert rows[0]["finished_at"] is not None
        # The temp file must not be left behind on failure.
        assert list(backups_dir.glob("*.tmp")) == []

    def test_success_leaves_no_temp_file(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        _insert_agent(db_path)

        run_backup(db_path, backups_dir)

        assert list(backups_dir.glob("*.tmp")) == []
        assert _today_backup_path(backups_dir).exists()


class TestMaybeDailyBackup:
    def test_runs_first_time_then_skips_immediate_second_call(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"

        first = maybe_daily_backup(db_path, backups_dir)
        second = maybe_daily_backup(db_path, backups_dir)

        assert first == _today_backup_path(backups_dir)
        assert first.exists()
        assert second is None
        rows = _backup_run_rows(db_path)
        assert [row["success"] for row in rows] == [1]

    def test_runs_again_when_last_success_is_older_than_twenty_four_hours(
        self, tmp_path: Path
    ) -> None:
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        old_ts = (datetime.now(UTC) - timedelta(days=2)).isoformat()

        first = maybe_daily_backup(db_path, backups_dir)
        with open_connection(db_path) as conn:
            conn.execute(
                "UPDATE backup_runs SET started_at = ?, finished_at = ?",
                (old_ts, old_ts),
            )

        second = maybe_daily_backup(db_path, backups_dir)

        assert first == _today_backup_path(backups_dir)
        assert second == _today_backup_path(backups_dir)
        rows = _backup_run_rows(db_path)
        assert [row["success"] for row in rows] == [1, 1]

    def test_abandoned_pending_run_does_not_suppress_backup(
        self, tmp_path: Path
    ) -> None:
        """Regression for review R6: a crashed run leaves finished_at NULL; once
        it ages past the pending window it must not block today's backup."""
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        stale_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()

        with open_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_runs (started_at, path, success) VALUES (?, ?, 0)",
                (stale_ts, str(_today_backup_path(backups_dir))),
            )

        result = maybe_daily_backup(db_path, backups_dir)

        assert result == _today_backup_path(backups_dir)
        assert result.exists()
        rows = _backup_run_rows(db_path)
        assert [row["success"] for row in rows] == [0, 1]

    def test_recent_pending_run_still_suppresses_backup(self, tmp_path: Path) -> None:
        """A genuine concurrent in-progress run (recent pending row) suppresses."""
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        backups_dir = data_dir / "backups"
        recent_ts = now_iso()

        with open_connection(db_path) as conn:
            conn.execute(
                "INSERT INTO backup_runs (started_at, path, success) VALUES (?, ?, 0)",
                (recent_ts, str(_today_backup_path(backups_dir))),
            )

        result = maybe_daily_backup(db_path, backups_dir)

        assert result is None
        rows = _backup_run_rows(db_path)
        assert [row["success"] for row in rows] == [0]


class TestPruneBackups:
    def test_deletes_old_dated_backups_and_keeps_recent_files(
        self, tmp_path: Path
    ) -> None:
        backups_dir = tmp_path / "backups"
        backups_dir.mkdir()
        old = backups_dir / "agent-memory-2000-01-01.db"
        recent = _today_backup_path(backups_dir)
        non_matching = backups_dir / "agent-memory-not-a-date.db"
        old.write_text("old", encoding="utf-8")
        recent.write_text("recent", encoding="utf-8")
        non_matching.write_text("ignore", encoding="utf-8")

        deleted = prune_backups(backups_dir)

        assert deleted == [old]
        assert not old.exists()
        assert recent.exists()
        assert non_matching.exists()


class TestLazyDailyBackup:
    def test_lazy_thread_runs_backup_and_prunes_old_snapshots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression for review R1: the automatic (lazy) path must enforce
        retention, not just the manual memory_backup_now tool."""
        data_dir = tmp_path / "data"
        init_db(data_dir)
        backups_dir = data_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        stale = backups_dir / "agent-memory-2000-01-01.db"
        stale.write_text("old", encoding="utf-8")

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(data_dir))

        thread = start_lazy_daily_backup()
        thread.join(timeout=10)

        assert not thread.is_alive()
        assert _today_backup_path(backups_dir).exists()
        assert not stale.exists()
