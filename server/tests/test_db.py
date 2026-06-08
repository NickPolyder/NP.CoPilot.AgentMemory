"""Tests for np_agent_memory.db — data directory and connection factory."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from np_agent_memory.db import (
    _DB_FILENAME,
    WalConversionError,
    configure_connection,
    connect,
    ensure_data_dir,
    get_data_dir,
    get_db_path,
    open_connection,
)


class TestGetDataDir:
    """Tests for get_data_dir() resolution logic."""

    def test_uses_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
    """Tests for configure_connection shared helper."""

    def test_applies_all_pragmas(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        configure_connection(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        # NORMAL = 1 in SQLite's numeric mapping
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        conn.close()

    def test_raises_when_wal_not_set(self) -> None:
        """If journal_mode does not end up as 'wal', configuration fails loudly
        with the retryable WalConversionError subtype (not a plain RuntimeError),
        so the migration runner's busy loop can retry cold-start contention."""

        class _Cursor:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        class _FakeConn:
            def execute(self, sql: str):
                # Report 'delete' for the journal_mode pragma (WAL refused).
                if "journal_mode" in sql:
                    return _Cursor(("delete",))
                return _Cursor(None)

        # Pin the exact subtype: a regression to plain RuntimeError would
        # silently disable the migration WAL-conversion retry path.
        assert issubclass(WalConversionError, RuntimeError)
        with pytest.raises(WalConversionError, match="WAL journal mode"):
            configure_connection(_FakeConn())

    def test_raises_when_wal_pragma_returns_no_row(self) -> None:
        """A missing journal_mode result row is treated as a (retryable) failure."""

        class _Cursor:
            def fetchone(self):
                return None

        class _FakeConn:
            def execute(self, sql: str):
                return _Cursor()

        with pytest.raises(WalConversionError, match="WAL journal mode"):
            configure_connection(_FakeConn())


class TestConnectFailureSafety:
    """connect() must not leak a descriptor when configuration fails."""

    def test_connect_closes_connection_on_config_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        closed = {"value": False}

        class _TrackingConn(sqlite3.Connection):
            def close(self):
                closed["value"] = True
                super().close()

        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            kwargs["factory"] = _TrackingConn
            return real_connect(*args, **kwargs)

        monkeypatch.setattr("np_agent_memory.db.sqlite3.connect", tracking_connect)
        monkeypatch.setattr(
            "np_agent_memory.db.configure_connection",
            lambda conn: (_ for _ in ()).throw(RuntimeError("config boom")),
        )

        with pytest.raises(RuntimeError, match="config boom"):
            connect(db_path)
        assert closed["value"], "connection was not closed after config failure"


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
        assert conn_ref is not None
        with pytest.raises(sqlite3.ProgrammingError):
            conn_ref.execute("SELECT 1")
