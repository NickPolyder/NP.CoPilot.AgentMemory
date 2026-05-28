"""Tests for np_agent_memory.db — data directory and connection factory."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from np_agent_memory.db import (
    _DB_FILENAME,
    _configure_connection,
    connect,
    ensure_data_dir,
    get_data_dir,
    get_db_path,
    init_db,
    open_connection,
)


class TestGetDataDir:
    """Tests for get_data_dir() resolution logic."""

    def test_uses_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        override = str(tmp_path / "custom-dir")
        monkeypatch.setenv("AGENT_MEMORY_DIR", override)
        assert get_data_dir() == Path(override)

    def test_rejects_relative_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_MEMORY_DIR", "relative/path")
        with pytest.raises(ValueError, match="must be an absolute path"):
            get_data_dir()

    def test_defaults_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
        result = get_data_dir()
        assert result == Path.home() / ".copilot" / "np-agent-memory"


class TestEnsureDataDir:
    """Tests for ensure_data_dir() folder creation."""

    def test_creates_all_subdirs(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "agent-memory"
        result = ensure_data_dir(data_dir)
        assert result == data_dir
        assert data_dir.is_dir()
        assert (data_dir / "backups").is_dir()
        assert (data_dir / "logs").is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        data_dir = tmp_path / "agent-memory"
        ensure_data_dir(data_dir)
        ensure_data_dir(data_dir)  # no error on re-run
        assert data_dir.is_dir()


class TestGetDbPath:
    """Tests for get_db_path()."""

    def test_returns_correct_filename(self, tmp_path: Path) -> None:
        result = get_db_path(tmp_path)
        assert result == tmp_path / _DB_FILENAME
        assert result.name == "agent-memory.db"


class TestConnect:
    """Tests for the connection factory."""

    def test_returns_configured_connection(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = connect(db_path)
        try:
            # Verify WAL mode
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"

            # Verify foreign keys enabled
            fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
            assert fk == 1

            # Verify busy timeout
            bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
            assert bt == 5000

            # Verify row factory
            assert conn.row_factory == sqlite3.Row
        finally:
            conn.close()

    def test_open_connection_context_manager(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        with open_connection(db_path) as conn:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            row = conn.execute("SELECT x FROM t").fetchone()
            assert row["x"] == 42


class TestConfigureConnection:
    """Tests for _configure_connection shared helper."""

    def test_applies_all_pragmas(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        _configure_connection(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # NORMAL = 1 in SQLite's numeric mapping
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        conn.close()


class TestInitDb:
    """Tests for the full init_db() flow."""

    def test_creates_db_and_applies_migrations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
            assert count == 1
            # Schema still intact after second run
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "agents" in tables
            assert "handovers" in tables


class TestOpenConnectionSafety:
    """Connection lifecycle and exception safety."""

    def test_connection_closed_on_exception(self, tmp_path: Path) -> None:
        """Context manager closes connection even when body raises."""
        db_path = tmp_path / "test.db"
        conn_ref = None
        with pytest.raises(RuntimeError):
            with open_connection(db_path) as conn:
                conn_ref = conn
                raise RuntimeError("boom")
        # Connection should be closed — executing on it should fail
        with pytest.raises(Exception):
            conn_ref.execute("SELECT 1")
