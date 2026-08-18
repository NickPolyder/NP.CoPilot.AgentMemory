"""Tests for np_agent_memory.__main__ — memory_alive tool and server entry."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from unittest.mock import patch

import pytest
from np_agent_memory.__main__ import mcp, memory_alive
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.inbox import inbox_ack, inbox_send


def _set_main_argv(
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["np-agent-memory", *args])


def _register_agent(
    tmp_path,
    conn: sqlite3.Connection,
    dirname: str,
    name: str,
) -> dict[str, str]:
    cwd = tmp_path / dirname
    cwd.mkdir()
    result = register_agent(conn, name=name, agent_cwd=str(cwd))
    return {"cwd": str(cwd), "canonical": result["canonical_path"], "name": name}


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

        _set_main_argv(monkeypatch)
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

        _set_main_argv(monkeypatch)
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

        _set_main_argv(monkeypatch)
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

        _set_main_argv(monkeypatch)
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

        _set_main_argv(monkeypatch)
        monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with patch(
            "np_agent_memory.__main__.init_db",
            side_effect=ValueError("boom"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                mod.main()
            assert exc_info.value.code == 1

    def test_main_prints_single_line_json_summary_for_inbox_summary_subcommand(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CLI summary path should emit one JSON object and avoid server mode."""
        import np_agent_memory.__main__ as mod

        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        with open_connection(db_path) as conn:
            sender = _register_agent(tmp_path, conn, "sender", "alice")
            recipient = _register_agent(tmp_path, conn, "recipient", "bob")
            normal_unread = inbox_send(
                conn,
                agent_cwd=sender["cwd"],
                to="bob",
                subject="normal",
                body="body-normal",
            )
            urgent_unread = inbox_send(
                conn,
                agent_cwd=sender["cwd"],
                to="bob",
                subject="urgent",
                body="body-urgent",
                priority="urgent",
            )
            read_message = inbox_send(
                conn,
                agent_cwd=sender["cwd"],
                to="bob",
                subject="read",
                body="body-read",
                priority="high",
            )
            inbox_ack(
                conn,
                agent_cwd=recipient["cwd"],
                message_ids=[read_message["id"]],
                status="read",
            )
            before = [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, read_at, acked_at FROM inbox ORDER BY id"
                ).fetchall()
            ]
        capsys.readouterr()

        _set_main_argv(
            monkeypatch,
            "inbox-summary",
            "--agent-cwd",
            recipient["cwd"],
        )
        monkeypatch.setenv("AGENT_MEMORY_DIR", str(data_dir))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with (
            patch.object(mcp, "run") as mock_run,
            patch("np_agent_memory.__main__.start_lazy_daily_backup") as mock_backup,
        ):
            mod.main()

        captured = capsys.readouterr()
        assert captured.err == ""
        assert len(captured.out.splitlines()) == 1
        assert json.loads(captured.out) == {
            "canonical_path": recipient["canonical"],
            "unread_count": 2,
            "urgent_unread_count": 1,
            "messages": [
                {"id": urgent_unread["id"], "priority": "urgent"},
                {"id": normal_unread["id"], "priority": "normal"},
            ],
        }
        mock_run.assert_not_called()
        mock_backup.assert_not_called()

        with open_connection(db_path) as conn:
            after = [
                tuple(row)
                for row in conn.execute(
                    "SELECT id, read_at, acked_at FROM inbox ORDER BY id"
                ).fetchall()
            ]
        assert after == before

    def test_main_inbox_summary_exits_with_diagnostic_for_unregistered_agent(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """CLI summary path should report registration failures on stderr."""
        import np_agent_memory.__main__ as mod

        data_dir = tmp_path / "data"
        init_db(data_dir)
        missing = tmp_path / "missing"
        missing.mkdir()
        capsys.readouterr()

        _set_main_argv(monkeypatch, "inbox-summary", "--agent-cwd", str(missing))
        monkeypatch.setenv("AGENT_MEMORY_DIR", str(data_dir))
        monkeypatch.setattr(mod, "_DB_PATH", mod._DB_PATH)

        with (
            patch.object(mcp, "run") as mock_run,
            patch("np_agent_memory.__main__.start_lazy_daily_backup") as mock_backup,
            pytest.raises(SystemExit) as exc_info,
        ):
            mod.main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert captured.out == ""
        assert "not registered" in captured.err
        mock_run.assert_not_called()
        mock_backup.assert_not_called()
