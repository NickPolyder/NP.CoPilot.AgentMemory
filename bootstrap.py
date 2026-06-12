"""Self-bootstrapping launcher for the np-agent-memory MCP server.

Runs under the *system* Python (invoked as ``py -3 bootstrap.py`` from
``.mcp.json``). It ensures a virtual environment with the pinned dependencies
exists in the runtime data directory, then hands off (same stdio pipes) to the
real server at ``python -m np_agent_memory``.

Why this exists: the Copilot CLI installs a plugin straight from a repo and does
NOT run any build/install hook, and a Python venv cannot be shipped prebuilt
(native wheels are OS/arch/Python-version specific). So the runtime is built on
the consumer's machine on first launch. The only prerequisite is Python 3.12+ on
PATH.

This module is intentionally **stdlib-only**: it must run before any third-party
package exists. Keep it dependency-free.

The runtime venv lives in the runtime data dir
(``$HOME/.copilot/np-agent-memory/.venv`` or ``$AGENT_MEMORY_DIR/.venv``), NOT in
the plugin install dir, so it survives ``copilot plugin update``/reinstall.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import venv
from contextlib import suppress
from pathlib import Path

MIN_PYTHON: tuple[int, int] = (3, 12)
MARKER_SCHEMA = 1

PLUGIN_ROOT = Path(__file__).resolve().parent
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"
SERVER_DIR = PLUGIN_ROOT / "server"

_VENV_DIRNAME = ".venv"
_MARKER_NAME = ".np-bootstrap.json"
_LOCK_NAME = ".np-bootstrap.lock"

# A build holding the lock longer than this is treated as crashed/abandoned and
# its lock is reclaimed. Generous: a cold venv + pip install of the pinned deps
# is seconds, not minutes.
_LOCK_STALE_SECONDS = 600
# How long a waiting process blocks for a concurrent build before giving up.
_WAIT_TIMEOUT_SECONDS = 300
_WAIT_POLL_SECONDS = 1.0


def _log(message: str) -> None:
    """Write a breadcrumb to stderr.

    The Copilot CLI captures plugin-server stderr into
    ``~/.copilot/logs/process-*.log`` under ``[mcp server np-agent-memory
    stderr]`` (see docs/spike-0.md §6), so this is the diagnostic channel when a
    cold start fails.
    """
    print(f"[np-agent-memory bootstrap] {message}", file=sys.stderr, flush=True)


def runtime_dir() -> Path:
    """Resolve the runtime data directory (mirrors ``db.get_data_dir``).

    Duplicated here on purpose: bootstrap runs before the package venv exists,
    so it cannot import ``np_agent_memory``.
    """
    override = os.environ.get("AGENT_MEMORY_DIR")
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ValueError(
                f"AGENT_MEMORY_DIR must be an absolute path, got: {override!r}"
            )
        return path
    return Path.home() / ".copilot" / "np-agent-memory"


def venv_python(venv_dir: Path) -> Path:
    """Return the venv's Python interpreter path for the current OS."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def requirements_hash() -> str:
    """Return a sha256 of ``requirements.txt`` used as the venv freshness key."""
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def venv_is_ready(venv_dir: Path) -> bool:
    """True when ``venv_dir`` has an interpreter and a matching deps marker."""
    if not venv_python(venv_dir).exists():
        return False
    marker = venv_dir / _MARKER_NAME
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("requirements_sha256") == requirements_hash()


def _create_venv(target: Path) -> None:
    """Create a fresh venv with pip at ``target`` (seam for tests)."""
    venv.EnvBuilder(with_pip=True, clear=True).create(target)


def _pip_install_requirements(python: Path) -> None:
    """Install the pinned requirements into a venv (seam for tests)."""
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-cache-dir",
            "-r",
            str(REQUIREMENTS),
        ],
        check=True,
    )


def _force_remove(path: Path) -> None:
    """Best-effort recursive removal of a file or directory."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        with suppress(OSError):
            path.unlink()


def build_venv(venv_dir: Path) -> None:
    """Build the venv into a temp dir and atomically swap it into place.

    Building into ``<name>.tmp-<pid>`` and ``os.replace``-ing it in keeps a
    partially-built venv from ever being observed as ready, and lets two racing
    processes settle on a whole venv (mirrors the backup temp+rename pattern).
    A relocated venv runs fine — CPython derives ``sys.prefix`` from the
    interpreter's location, not ``pyvenv.cfg`` (docs/spike-0.md §6).
    """
    tmp = venv_dir.with_name(f"{venv_dir.name}.tmp-{os.getpid()}")
    _force_remove(tmp)
    _log(f"building runtime venv at {venv_dir} (first launch or deps changed)...")
    try:
        _create_venv(tmp)
        _pip_install_requirements(venv_python(tmp))
        marker = {"requirements_sha256": requirements_hash(), "schema": MARKER_SCHEMA}
        (tmp / _MARKER_NAME).write_text(json.dumps(marker), encoding="utf-8")
        if venv_dir.exists():
            stale = venv_dir.with_name(f"{venv_dir.name}.old-{os.getpid()}")
            _force_remove(stale)
            os.replace(venv_dir, stale)
            _force_remove(stale)
        os.replace(tmp, venv_dir)
    except BaseException:
        _force_remove(tmp)
        raise
    _log("runtime venv ready.")


def _lock_is_stale(lock: Path) -> bool:
    """True when a build lock is older than ``_LOCK_STALE_SECONDS``."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    return age > _LOCK_STALE_SECONDS


def ensure_venv(runtime: Path, venv_dir: Path) -> None:
    """Ensure ``venv_dir`` is ready, building it under a cross-process lock.

    The build lock is a directory (``mkdir`` is atomic). The winner builds; other
    CLI windows wait for the venv to become ready rather than building their own.
    A crashed builder's stale lock is reclaimed after ``_LOCK_STALE_SECONDS``.
    """
    lock = runtime / _LOCK_NAME
    deadline = time.time() + _WAIT_TIMEOUT_SECONDS
    while True:
        if venv_is_ready(venv_dir):
            return
        try:
            lock.mkdir()
        except FileExistsError:
            if _lock_is_stale(lock):
                _log("reclaiming stale build lock from a crashed launcher.")
                _force_remove(lock)
                continue
            if time.time() > deadline:
                raise TimeoutError(
                    "timed out waiting for another launcher to build the venv."
                ) from None
            time.sleep(_WAIT_POLL_SECONDS)
            continue
        try:
            if not venv_is_ready(venv_dir):
                build_venv(venv_dir)
        finally:
            _force_remove(lock)
        return


def exec_server(venv_dir: Path) -> int:
    """Run ``python -m np_agent_memory`` in the venv with inherited stdio.

    Inherited stdio means the venv interpreter talks to the CLI over the exact
    pipes the bootstrap was given, so the MCP stdio handshake is unaffected. The
    bootstrap stays alive as a thin parent for the session and forwards the exit
    code.
    """
    python = venv_python(venv_dir)
    env = dict(os.environ)
    server_path = str(SERVER_DIR)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = server_path + os.pathsep + existing if existing else server_path
    env["PYTHONUNBUFFERED"] = "1"
    completed = subprocess.run([str(python), "-m", "np_agent_memory"], env=env)
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    """Validate the runtime, ensure the venv, then hand off to the server.

    With ``--ensure-only`` the runtime venv is built/refreshed and the function
    returns without launching the server (used by ``install.ps1`` to pre-warm
    the runtime so a consumer's first session is instant).
    """
    args = sys.argv[1:] if argv is None else argv
    ensure_only = "--ensure-only" in args

    if sys.version_info < MIN_PYTHON:
        _log(
            f"FATAL: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required on PATH; "
            f"this launcher is running "
            f"{sys.version_info.major}.{sys.version_info.minor}. Install a newer "
            f"Python (ensure `py -3` resolves to it) and restart the CLI."
        )
        return 1
    if not REQUIREMENTS.exists():
        _log(f"FATAL: requirements.txt not found at {REQUIREMENTS}.")
        return 1

    try:
        runtime = runtime_dir()
    except ValueError as exc:
        _log(f"FATAL: {exc}")
        return 1

    venv_dir = runtime / _VENV_DIRNAME
    if not venv_is_ready(venv_dir):
        runtime.mkdir(parents=True, exist_ok=True)
        try:
            ensure_venv(runtime, venv_dir)
        except (TimeoutError, subprocess.CalledProcessError, OSError) as exc:
            _log(f"FATAL: could not provision the runtime venv: {exc!r}")
            return 1

    if ensure_only:
        _log(f"runtime venv ready at {venv_dir} (--ensure-only; not launching).")
        return 0

    return exec_server(venv_dir)


if __name__ == "__main__":
    raise SystemExit(main())
