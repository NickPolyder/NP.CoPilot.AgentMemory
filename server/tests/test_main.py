"""Tests for np_agent_memory.__main__ — memory_alive tool and server entry."""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import patch

import pytest
from np_agent_memory.__main__ import mcp, memory_alive


class TestMemoryAliveTool:
    """Tests for the memory_alive() liveness probe."""

    def test_returns_expected_keys(self) -> None:
        result = memory_alive()
        expected_keys = {
            "server_name",
            "package_version",
            "mcp_sdk_version",
            "pid",
            "executable",
            "started_at_iso",
            "uptime_seconds",
            "db_path",
        }
        assert set(result.keys()) == expected_keys

    def test_key_types(self) -> None:
        result = memory_alive()
        assert isinstance(result["server_name"], str)
        assert isinstance(result["package_version"], str)
        assert isinstance(result["mcp_sdk_version"], str)
        assert isinstance(result["pid"], int)
        assert isinstance(result["executable"], str)
        assert isinstance(result["started_at_iso"], str)
        assert isinstance(result["uptime_seconds"], (int, float))
        # db_path is None before main() runs, or str after
        assert result["db_path"] is None or isinstance(result["db_path"], str)

    def test_db_path_none_before_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Before main() is called, db_path should be None."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setattr(mod, "_DB_PATH", None)
        result = memory_alive()
        assert result["db_path"] is None

    def test_uptime_increases(self) -> None:
        """uptime_seconds should increase between calls."""
        r1 = memory_alive()
        time.sleep(0.05)
        r2 = memory_alive()
        assert r2["uptime_seconds"] >= r1["uptime_seconds"]

    def test_server_name_matches_mcp(self) -> None:
        result = memory_alive()
        assert result["server_name"] == "np-agent-memory"

    def test_pid_is_current_process(self) -> None:
        import os

        result = memory_alive()
        assert result["pid"] == os.getpid()


class TestMainFunction:
    """Tests for the main() startup sequence."""

    def test_main_initializes_db_and_calls_run(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should initialize the DB, set _DB_PATH, and call mcp.run()."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        # Restore _DB_PATH after the test so it doesn't leak into other tests.
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch.object(mcp, "run") as mock_run:
            mod.main()

            # DB path was set and file exists
            assert mod._DB_PATH is not None
            assert mod._DB_PATH.exists()

            # Schema was actually applied
            conn = sqlite3.connect(str(mod._DB_PATH))
            try:
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "agents" in tables
                assert "migrations" in tables
            finally:
                conn.close()

            # mcp.run() was invoked
            mock_run.assert_called_once()

    def test_main_exits_on_oserror(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should exit with code 1 on a filesystem (OSError) failure."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch(
            "np_agent_memory.__main__.init_db", side_effect=OSError("disk full")
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_exits_on_runtime_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should exit with code 1 on a RuntimeError (e.g. WAL failure)."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch(
            "np_agent_memory.__main__.init_db",
            side_effect=RuntimeError("WAL not set"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_exits_on_sqlite_operational_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should exit with code 1 on a sqlite OperationalError."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch(
            "np_agent_memory.__main__.init_db",
            side_effect=sqlite3.OperationalError("unable to open database file"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_exits_on_unexpected_error(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """main() should exit with code 1 on any unexpected exception."""
        import np_agent_memory.__main__ as mod

        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch(
            "np_agent_memory.__main__.init_db",
            side_effect=ValueError("boom"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1
