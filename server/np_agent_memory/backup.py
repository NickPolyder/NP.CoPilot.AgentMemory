"""SQLite online backup helpers and MCP tool registration.

Backups are server-scoped operational maintenance: they do not resolve an
agent identity and never depend on ``agent_cwd``. The public helpers take
explicit paths so tests can build isolated temp databases, mirroring
``startup.init_db``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from np_agent_memory.db import connect, ensure_data_dir, get_db_path, run_in_write_txn
from np_agent_memory.identity import now_iso

_BACKUP_PREFIX = "agent-memory-"
_BACKUP_SUFFIX = ".db"
_RECENT_BACKUP_WINDOW = timedelta(hours=24)
# A genuine in-progress backup finishes in seconds. Only treat a pending
# (``finished_at IS NULL``) run as "someone else is backing up right now" if it
# started within this window; an older pending row is an abandoned/crashed run
# and must not suppress today's backup (see review R6).
_PENDING_BACKUP_WINDOW = timedelta(minutes=5)


def _backup_path(backups_dir: Path) -> Path:
    """Return today's backup file path inside ``backups_dir``."""
    return backups_dir / f"{_BACKUP_PREFIX}{date.today().isoformat()}{_BACKUP_SUFFIX}"


def _insert_backup_run(db_path: Path, path: Path) -> int:
    """Insert a pending backup run row and return its integer id."""
    started_at = now_iso()

    def _work(c: sqlite3.Connection) -> int:
        cur = c.execute(
            "INSERT INTO backup_runs (started_at, path, success) VALUES (?, ?, 0)",
            (started_at, str(path)),
        )
        return int(cur.lastrowid)

    conn = connect(db_path)
    try:
        return run_in_write_txn(conn, _work)
    finally:
        conn.close()


def _finish_backup_run(db_path: Path, run_id: int, *, success: bool) -> None:
    """Mark a pending backup run as finished."""
    finished_at = now_iso()

    def _work(c: sqlite3.Connection) -> None:
        c.execute(
            "UPDATE backup_runs SET finished_at = ?, success = ? WHERE id = ?",
            (finished_at, 1 if success else 0, run_id),
        )

    conn = connect(db_path)
    try:
        run_in_write_txn(conn, _work)
    finally:
        conn.close()


def _perform_online_backup(db_path: Path, dest_path: Path) -> None:
    """Copy ``db_path`` to ``dest_path`` using SQLite's online backup API.

    The copy is written to a unique temporary file in the destination directory
    and then atomically renamed into place. Because the pending-row throttle in
    :func:`maybe_daily_backup` only suppresses *recent* in-progress runs (see
    ``_PENDING_BACKUP_WINDOW``), two processes could in principle target the same
    dated snapshot at once; the temp-file + ``os.replace`` keeps each writer's
    output isolated and the final file always whole.
    """
    tmp_path = dest_path.with_name(f"{dest_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        src = sqlite3.connect(str(db_path))
        try:
            dest = sqlite3.connect(str(tmp_path))
            try:
                src.backup(dest)
            finally:
                dest.close()
        finally:
            src.close()
        os.replace(tmp_path, dest_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _run_backup_with_run_id(
    db_path: Path, backups_dir: Path, run_id: int, dest_path: Path
) -> Path:
    """Perform the backup and update the pre-created ``backup_runs`` row."""
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
        _perform_online_backup(db_path, dest_path)
    except Exception:
        _finish_backup_run(db_path, run_id, success=False)
        raise

    _finish_backup_run(db_path, run_id, success=True)
    return dest_path


def run_backup(db_path: Path, backups_dir: Path) -> Path:
    """Force an online SQLite backup into today's snapshot file.

    Records a ``backup_runs`` row before the backup starts, marks it successful
    after the copy completes, and records ``success=0`` if the copy fails after
    the run row has been claimed.
    """
    dest_path = _backup_path(backups_dir)
    run_id = _insert_backup_run(db_path, dest_path)
    return _run_backup_with_run_id(db_path, backups_dir, run_id, dest_path)


def maybe_daily_backup(db_path: Path, backups_dir: Path) -> Path | None:
    """Run a backup unless a successful one completed in the last 24 hours.

    The throttle check and pending-row claim happen in one ``BEGIN IMMEDIATE``
    transaction. A *recent* concurrent in-progress run also causes a skip so
    multiple MCP server processes do not perform the same daily backup
    simultaneously; an abandoned pending run (crashed before finishing) is
    ignored once it ages past ``_PENDING_BACKUP_WINDOW``.
    """
    dest_path = _backup_path(backups_dir)
    now = datetime.now(UTC)
    cutoff = (now - _RECENT_BACKUP_WINDOW).isoformat()
    pending_cutoff = (now - _PENDING_BACKUP_WINDOW).isoformat()
    started_at = now_iso()

    def _work(c: sqlite3.Connection) -> int | None:
        recent = c.execute(
            "SELECT id FROM backup_runs "
            "WHERE (success = 1 AND started_at >= ?) "
            "   OR (finished_at IS NULL AND started_at >= ?) "
            "ORDER BY started_at DESC LIMIT 1",
            (cutoff, pending_cutoff),
        ).fetchone()
        if recent is not None:
            return None

        cur = c.execute(
            "INSERT INTO backup_runs (started_at, path, success) VALUES (?, ?, 0)",
            (started_at, str(dest_path)),
        )
        return int(cur.lastrowid)

    conn = connect(db_path)
    try:
        run_id = run_in_write_txn(conn, _work)
    finally:
        conn.close()

    if run_id is None:
        return None
    return _run_backup_with_run_id(db_path, backups_dir, run_id, dest_path)


def prune_backups(backups_dir: Path, keep_days: int = 14) -> list[Path]:
    """Delete dated backup files older than ``keep_days`` and return them."""
    if keep_days < 1:
        raise ValueError("keep_days must be at least 1.")
    if not backups_dir.exists():
        return []

    cutoff = date.today() - timedelta(days=keep_days)
    deleted: list[Path] = []
    for path in backups_dir.glob(f"{_BACKUP_PREFIX}*{_BACKUP_SUFFIX}"):
        date_part = path.name[len(_BACKUP_PREFIX) : -len(_BACKUP_SUFFIX)]
        try:
            backup_date = date.fromisoformat(date_part)
        except ValueError:
            continue
        if backup_date >= cutoff:
            continue
        path.unlink()
        deleted.append(path)
    return deleted


def start_lazy_daily_backup() -> threading.Thread:
    """Run the throttled daily backup off the critical startup path.

    Spawns a best-effort daemon thread so a cold start is never delayed by the
    backup, and a backup failure can never crash the server. The 24h throttle
    in :func:`maybe_daily_backup` keeps this to at most one snapshot per day
    even though it is invoked on every server launch.
    """

    def _run() -> None:
        try:
            data_dir = ensure_data_dir()
            backups_dir = data_dir / "backups"
            created = maybe_daily_backup(get_db_path(data_dir), backups_dir)
            if created is not None:
                # Enforce the documented retention on the automatic path too;
                # the manual memory_backup_now tool already prunes after a run.
                prune_backups(backups_dir)
        except Exception as exc:  # pragma: no cover - best-effort background task
            print(
                f"[np-agent-memory] lazy daily backup skipped: {exc!r}",
                file=sys.stderr,
                flush=True,
            )

    thread = threading.Thread(target=_run, name="np-agent-memory-backup", daemon=True)
    thread.start()
    return thread


def register_backup_tools(mcp: FastMCP) -> None:
    """Register the backup tool on the FastMCP server."""

    @mcp.tool()
    def memory_backup_now() -> dict[str, Any]:
        """Force an immediate server-scoped SQLite backup.

        Returns:
            A dict with the backup path, success flag, and number of old
            snapshots pruned by retention.
        """
        data_dir = ensure_data_dir()
        db_path = get_db_path(data_dir)
        backup_path = run_backup(db_path, data_dir / "backups")
        pruned = prune_backups(data_dir / "backups")
        return {"path": str(backup_path), "success": True, "pruned": len(pruned)}
