"""Tests for np_agent_memory.startup — DB init orchestration."""

from __future__ import annotations

from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.migrations import _discover_migrations
from np_agent_memory.startup import init_db


class TestInitDb:
    """Tests for the full init_db() flow."""

    def test_creates_db_and_applies_migrations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        db_path = init_db(tmp_path)

        assert db_path.exists()
        assert (tmp_path / "backups").is_dir()
        assert (tmp_path / "logs").is_dir()

        # Verify schema was applied
        with open_connection(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            expected = {
                "agents",
                "agent_aliases",
                "notes",
                "todos",
                "blockers",
                "inbox",
                "handovers",
                "backup_runs",
                "migrations",
            }
            assert expected.issubset(tables)

    def test_idempotent_reruns(self, tmp_path: Path) -> None:
        db_path = init_db(tmp_path)
        db_path_2 = init_db(tmp_path)
        assert db_path == db_path_2

        with open_connection(db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == len(_discover_migrations())
            # Schema still intact after second run
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "agents" in tables
            assert "handovers" in tables
