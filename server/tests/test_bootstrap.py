"""Tests for the self-bootstrapping launcher (``bootstrap.py``).

The launcher lives at the repo root (referenced by ``.mcp.json`` as
``${PLUGIN_ROOT}/bootstrap.py``), so it is loaded here by file path rather than
imported as a package module. The heavy steps (``_create_venv`` /
``_pip_install_requirements``) are monkeypatched so these tests stay fast and
offline; the orchestration logic (freshness marker, atomic swap, cross-process
lock, exec env) is what we exercise.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_BOOTSTRAP_PATH = Path(__file__).resolve().parents[2] / "bootstrap.py"


def _load_bootstrap() -> ModuleType:
    spec = importlib.util.spec_from_file_location("np_bootstrap", _BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _load_bootstrap()


def _fake_venv(target: Path) -> None:
    """Create a minimal directory that looks like a built venv interpreter."""
    py = bootstrap.venv_python(target)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!fake-python", encoding="utf-8")


def _make_ready_venv(venv_dir: Path) -> None:
    """Create a venv-shaped dir with a marker matching the current requirements."""
    _fake_venv(venv_dir)
    marker = {
        "requirements_sha256": bootstrap.requirements_hash(),
        "schema": bootstrap.MARKER_SCHEMA,
    }
    (venv_dir / bootstrap._MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")


# ---------------------------------------------------------------------------
# runtime_dir
# ---------------------------------------------------------------------------


def test_runtime_dir_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENT_MEMORY_DIR", raising=False)
    monkeypatch.setattr(bootstrap.Path, "home", staticmethod(lambda: tmp_path))
    assert bootstrap.runtime_dir() == tmp_path / ".copilot" / "np-agent-memory"


def test_runtime_dir_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "custom"
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(override))
    assert bootstrap.runtime_dir() == override


def test_runtime_dir_relative_override_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_MEMORY_DIR", "relative/path")
    with pytest.raises(ValueError, match="absolute path"):
        bootstrap.runtime_dir()


# ---------------------------------------------------------------------------
# requirements_hash / venv_python
# ---------------------------------------------------------------------------


def test_requirements_hash_is_stable_and_file_derived() -> None:
    import hashlib

    expected = hashlib.sha256(bootstrap.REQUIREMENTS.read_bytes()).hexdigest()
    assert bootstrap.requirements_hash() == expected


def test_venv_python_os_specific(tmp_path: Path) -> None:
    py = bootstrap.venv_python(tmp_path)
    if sys.platform == "win32":
        assert py == tmp_path / "Scripts" / "python.exe"
    else:
        assert py == tmp_path / "bin" / "python"


# ---------------------------------------------------------------------------
# venv_is_ready
# ---------------------------------------------------------------------------


def test_venv_is_ready_false_when_missing(tmp_path: Path) -> None:
    assert bootstrap.venv_is_ready(tmp_path / "nope") is False


def test_venv_is_ready_false_without_marker(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    _fake_venv(venv_dir)
    assert bootstrap.venv_is_ready(venv_dir) is False


def test_venv_is_ready_false_on_hash_mismatch(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    _fake_venv(venv_dir)
    (venv_dir / bootstrap._MARKER_NAME).write_text(
        json.dumps({"requirements_sha256": "stale", "schema": 1}), encoding="utf-8"
    )
    assert bootstrap.venv_is_ready(venv_dir) is False


def test_venv_is_ready_false_on_corrupt_marker(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    _fake_venv(venv_dir)
    (venv_dir / bootstrap._MARKER_NAME).write_text("not json", encoding="utf-8")
    assert bootstrap.venv_is_ready(venv_dir) is False


def test_venv_is_ready_true_when_marker_matches(tmp_path: Path) -> None:
    venv_dir = tmp_path / ".venv"
    _make_ready_venv(venv_dir)
    assert bootstrap.venv_is_ready(venv_dir) is True


# ---------------------------------------------------------------------------
# build_venv
# ---------------------------------------------------------------------------


def test_build_venv_creates_ready_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bootstrap, "_create_venv", _fake_venv)
    monkeypatch.setattr(bootstrap, "_pip_install_requirements", lambda python: None)

    venv_dir = tmp_path / ".venv"
    bootstrap.build_venv(venv_dir)

    assert bootstrap.venv_is_ready(venv_dir) is True
    # No temp/old dirs left behind.
    leftovers = [p for p in tmp_path.iterdir() if p.name != ".venv"]
    assert leftovers == []


def test_build_venv_replaces_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bootstrap, "_create_venv", _fake_venv)
    monkeypatch.setattr(bootstrap, "_pip_install_requirements", lambda python: None)

    venv_dir = tmp_path / ".venv"
    _fake_venv(venv_dir)
    (venv_dir / "stale-file.txt").write_text("old", encoding="utf-8")

    bootstrap.build_venv(venv_dir)

    assert bootstrap.venv_is_ready(venv_dir) is True
    assert not (venv_dir / "stale-file.txt").exists()


def test_build_venv_cleans_temp_and_keeps_existing_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bootstrap, "_create_venv", _fake_venv)

    def _boom(python: Path) -> None:
        raise subprocess.CalledProcessError(1, ["pip"])

    monkeypatch.setattr(bootstrap, "_pip_install_requirements", _boom)

    venv_dir = tmp_path / ".venv"
    _make_ready_venv(venv_dir)  # a previously-good venv must survive a failed rebuild

    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.build_venv(venv_dir)

    # Existing venv untouched, no temp dir leaked.
    assert bootstrap.venv_is_ready(venv_dir) is True
    leftovers = [p for p in tmp_path.iterdir() if p.name != ".venv"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# ensure_venv (cross-process lock)
# ---------------------------------------------------------------------------


def test_ensure_venv_skips_build_when_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / ".venv"
    _make_ready_venv(venv_dir)

    def _fail(_venv_dir: Path) -> None:
        raise AssertionError("build_venv should not be called when venv is ready")

    monkeypatch.setattr(bootstrap, "build_venv", _fail)
    bootstrap.ensure_venv(tmp_path, venv_dir)


def test_ensure_venv_builds_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / ".venv"
    calls: list[Path] = []

    def _fake_build(target: Path) -> None:
        calls.append(target)
        _make_ready_venv(target)

    monkeypatch.setattr(bootstrap, "build_venv", _fake_build)
    bootstrap.ensure_venv(tmp_path, venv_dir)

    assert calls == [venv_dir]
    assert not (tmp_path / bootstrap._LOCK_NAME).exists()  # lock released


def test_ensure_venv_reclaims_stale_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / ".venv"
    lock = tmp_path / bootstrap._LOCK_NAME
    lock.mkdir()
    # Make the lock look abandoned.
    monkeypatch.setattr(bootstrap, "_lock_is_stale", lambda _lock: True)

    def _fake_build(target: Path) -> None:
        _make_ready_venv(target)

    monkeypatch.setattr(bootstrap, "build_venv", _fake_build)
    bootstrap.ensure_venv(tmp_path, venv_dir)

    assert bootstrap.venv_is_ready(venv_dir) is True
    assert not lock.exists()


def test_ensure_venv_times_out_on_held_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    venv_dir = tmp_path / ".venv"
    (tmp_path / bootstrap._LOCK_NAME).mkdir()  # held, not stale
    monkeypatch.setattr(bootstrap, "_lock_is_stale", lambda _lock: False)
    monkeypatch.setattr(bootstrap, "_WAIT_TIMEOUT_SECONDS", -1)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _s: None)

    with pytest.raises(TimeoutError):
        bootstrap.ensure_venv(tmp_path, venv_dir)


# ---------------------------------------------------------------------------
# exec_server
# ---------------------------------------------------------------------------


def test_exec_server_sets_env_and_returns_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _fake_run(cmd, env):  # noqa: ANN001
        captured["cmd"] = cmd
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run)

    venv_dir = tmp_path / ".venv"
    rc = bootstrap.exec_server(venv_dir)

    assert rc == 0
    cmd = captured["cmd"]
    assert cmd[0] == str(bootstrap.venv_python(venv_dir))
    assert cmd[1:] == ["-m", "np_agent_memory"]
    env = captured["env"]
    assert str(bootstrap.SERVER_DIR) in env["PYTHONPATH"]
    assert env["PYTHONUNBUFFERED"] == "1"


def test_exec_server_prepends_existing_pythonpath(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import os

    captured: dict[str, object] = {}
    monkeypatch.setenv("PYTHONPATH", "/pre/existing")

    def _fake_run(cmd, env):  # noqa: ANN001
        captured["env"] = env
        return subprocess.CompletedProcess(cmd, 7)

    monkeypatch.setattr(bootstrap.subprocess, "run", _fake_run)

    rc = bootstrap.exec_server(tmp_path / ".venv")
    assert rc == 7
    env = captured["env"]
    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == str(bootstrap.SERVER_DIR)
    assert "/pre/existing" in parts


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_rejects_old_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap, "MIN_PYTHON", (99, 0))
    assert bootstrap.main() == 1


def test_main_rejects_missing_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(bootstrap, "REQUIREMENTS", tmp_path / "absent.txt")
    assert bootstrap.main() == 1


def test_main_rejects_relative_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_MEMORY_DIR", "relative")
    assert bootstrap.main() == 1


def test_main_happy_path_execs_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(tmp_path / "runtime"))

    def _fake_ensure(runtime: Path, venv_dir: Path) -> None:
        _make_ready_venv(venv_dir)

    monkeypatch.setattr(bootstrap, "ensure_venv", _fake_ensure)
    monkeypatch.setattr(bootstrap, "exec_server", lambda venv_dir: 0)

    assert bootstrap.main() == 0


def test_main_warm_path_skips_ensure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    venv_dir = runtime / ".venv"
    _make_ready_venv(venv_dir)
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(runtime))

    def _fail(runtime: Path, venv_dir: Path) -> None:
        raise AssertionError("ensure_venv should be skipped on the warm path")

    monkeypatch.setattr(bootstrap, "ensure_venv", _fail)
    monkeypatch.setattr(bootstrap, "exec_server", lambda venv_dir: 0)

    assert bootstrap.main() == 0


def test_main_ensure_only_does_not_launch_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("AGENT_MEMORY_DIR", str(runtime))

    def _fake_ensure(runtime: Path, venv_dir: Path) -> None:
        _make_ready_venv(venv_dir)

    monkeypatch.setattr(bootstrap, "ensure_venv", _fake_ensure)

    def _fail(venv_dir: Path) -> int:
        raise AssertionError("exec_server must not run under --ensure-only")

    monkeypatch.setattr(bootstrap, "exec_server", _fail)

    assert bootstrap.main(["--ensure-only"]) == 0
