"""Tests for np_agent_memory.migrations — migration runner correctness."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from np_agent_memory.migrations import (
    _apply_migration,
    _checksum,
    _discover_migrations,
    _is_busy_error,
    _validate_migration_sql,
    run_migrations,
)

_MIGRATIONS_TABLE_DDL = (
    "CREATE TABLE IF NOT EXISTS migrations ("
    "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, "
    "applied_at TEXT NOT NULL)"
)


class TestDiscoverMigrations:
    """Tests for _discover_migrations() file discovery."""

    def test_finds_sql_files_in_order(self) -> None:
        migrations = _discover_migrations()
        assert len(migrations) >= 1
        assert migrations[0][0] == 1
        assert migrations[0][1].name == "0001_init.sql"

    def test_versions_are_ascending(self) -> None:
        migrations = _discover_migrations()
        versions = [v for v, _ in migrations]
        assert versions == sorted(versions)

    def test_sorts_scrambled_versions_by_integer(self, tmp_path: Path) -> None:
        """Discovery sorts by parsed integer version, not filename string order,
        and ignores files that don't match the NNNN_*.sql pattern."""
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        # Created out of order; a 5-digit version must sort after 4-digit ones.
        names = ("0002_b.sql", "0001_a.sql", "00010_j.sql", "notes.txt", "skip.sql")
        for name in names:
            (mig_dir / name).write_text("select 1;", encoding="utf-8")

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            migrations = _discover_migrations()

        assert [v for v, _ in migrations] == [1, 2, 10]


class TestChecksum:
    """Tests for _checksum() reproducibility and normalization."""

    def test_deterministic(self) -> None:
        sql = "CREATE TABLE t (x INT);\n"
        assert _checksum(sql) == _checksum(sql)

    def test_strips_trailing_whitespace(self) -> None:
        assert _checksum("SELECT 1") == _checksum("SELECT 1   \n\n")

    def test_content_sensitive(self) -> None:
        assert _checksum("SELECT 1") != _checksum("SELECT 2")

    def test_normalizes_crlf_to_lf(self) -> None:
        """Windows CRLF and Unix LF produce the same checksum."""
        lf_sql = "CREATE TABLE t (\n  id INT\n);\n"
        crlf_sql = "CREATE TABLE t (\r\n  id INT\r\n);\r\n"
        assert _checksum(lf_sql) == _checksum(crlf_sql)

    def test_normalizes_bare_cr(self) -> None:
        """Old Mac-style CR line endings also normalize."""
        lf_sql = "SELECT 1\nSELECT 2"
        cr_sql = "SELECT 1\rSELECT 2"
        assert _checksum(lf_sql) == _checksum(cr_sql)


class TestValidateMigrationSql:
    """Tests for the completeness guard that runs before applying SQL."""

    def test_complete_sql_passes(self) -> None:
        _validate_migration_sql("create table t (x);", "0001_t.sql")

    def test_trailing_statement_without_semicolon_passes(self) -> None:
        _validate_migration_sql("create table t (x)", "0001_t.sql")

    def test_comment_only_passes(self) -> None:
        _validate_migration_sql("-- just a comment", "0001_t.sql")

    def test_empty_passes(self) -> None:
        _validate_migration_sql("   \n  ", "0001_t.sql")

    def test_unterminated_block_comment_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="incomplete SQL"):
            _validate_migration_sql("create table t (x);\n/* oops", "0001_t.sql")

    def test_unterminated_string_rejected(self) -> None:
        with pytest.raises(RuntimeError, match="incomplete SQL"):
            _validate_migration_sql("insert into t values ('foo", "0001_t.sql")


class TestExecuteScriptApply:
    """Behaviors that depend on SQLite's own parser (executescript path)."""

    def _migrations_table(self, db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_MIGRATIONS_TABLE_DDL)
        conn.close()

    def test_trigger_with_internal_semicolons_applied(self, tmp_path: Path) -> None:
        """A CREATE TRIGGER body contains semicolons the old splitter mishandled."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        sql = (
            "create table t (x integer, y integer);\n"
            "create trigger trg after insert on t begin\n"
            "  update t set y = 1 where x = new.x;\n"
            "end;"
        )
        _apply_migration(db_path, 2, sql, _checksum(sql), "0002_trigger.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            objs = {
                r[0]
                for r in conn.execute(
                    "select name from sqlite_master where type in ('table','trigger')"
                ).fetchall()
            }
            assert "t" in objs and "trg" in objs
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_quoted_identifier_with_semicolon_applied(self, tmp_path: Path) -> None:
        """A double-quoted identifier containing a semicolon must survive."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        sql = 'create table "weird;name" (x integer);'
        _apply_migration(db_path, 2, sql, _checksum(sql), "0002_quoted.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            names = {
                r[0]
                for r in conn.execute(
                    "select name from sqlite_master where type='table'"
                ).fetchall()
            }
            assert "weird;name" in names
        finally:
            conn.close()

    def test_no_trailing_semicolon_applied(self, tmp_path: Path) -> None:
        """A final statement without a terminating semicolon still applies."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        sql = "create table t (x integer)"
        _apply_migration(db_path, 2, sql, _checksum(sql), "0002_no_semi.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name='t'"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "body",
        [
            "create table t (x integer);\ncommit;\ncreate table u (y integer);",
            "create table t (x integer);\nrollback;\ncreate table u (y integer);",
            "create table t (x integer);\nend;\ncreate table u (y integer);",
            "create table t (x integer);\nbegin;\ncreate table u (y integer);",
            "create table t (x integer);\nsavepoint sp;\n"
            "create table u (y integer);\nrelease sp;",
            "create table t (x integer);\n"
            "attach database ':memory:' as aux;\ncreate table u (y integer);",
            "create table t (x integer);\ndetach database aux;\n"
            "create table u (y integer);",
        ],
        ids=[
            "commit",
            "rollback",
            "end",
            "begin",
            "savepoint-release",
            "attach",
            "detach",
        ],
    )
    def test_transaction_control_in_body_rejected(
        self, tmp_path: Path, body: str
    ) -> None:
        """Any transaction-control or ATTACH/DETACH statement is rejected at
        prepare time and leaves NO partial schema behind (the authorizer denies
        it before it can commit, so the outer rollback undoes all DDL)."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        with pytest.raises(
            RuntimeError,
            match="forbidden statement|closed its own transaction",
        ):
            _apply_migration(db_path, 2, body, _checksum(body), "0002_txn.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            # The migration must not be recorded after the guard fires.
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 0
            )
            # No partial schema may leak: neither table created in the body
            # (before or after the transaction-control statement) persists.
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name in ('t', 'u')"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

    def test_file_attach_rejected_and_external_file_not_created(
        self, tmp_path: Path
    ) -> None:
        """A file-based ATTACH is denied at prepare time, validating the
        authorizer's rationale: the external database file is never created, so
        nothing escapes the runner-owned transaction's rollback boundary."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        aux_path = tmp_path / "aux.db"
        body = (
            "create table t (x integer);\n"
            f"attach database '{aux_path.as_posix()}' as aux;\n"
            "create table u (y integer);"
        )
        with pytest.raises(RuntimeError, match="forbidden statement"):
            _apply_migration(db_path, 2, body, _checksum(body), "0002_attach.sql")

        assert not aux_path.exists(), "ATTACH must not create the external file"

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name in ('t', 'u')"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

    def test_vacuum_in_body_fails_and_rolls_back(self, tmp_path: Path) -> None:
        """VACUUM cannot run inside a transaction, so a migration body using it
        fails and records nothing — pinning the documented 'no out-of-transaction
        statements' rule (the body runs inside the runner-owned BEGIN IMMEDIATE)."""
        db_path = tmp_path / "test.db"
        self._migrations_table(db_path)
        body = "create table t (x integer);\nvacuum;"
        with pytest.raises(sqlite3.OperationalError):
            _apply_migration(db_path, 2, body, _checksum(body), "0002_vacuum.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name = 't'"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

    def test_bom_prefixed_file_applies_and_matches_checksum(
        self, tmp_path: Path
    ) -> None:
        """A UTF-8 BOM is stripped on read, so a BOM file applies cleanly and
        its recorded checksum matches the BOM-free body."""
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        body = "create table t (x integer);\n"
        (mig_dir / "0001_init.sql").write_text(body, encoding="utf-8-sig")
        db_path = tmp_path / "test.db"

        import np_agent_memory.migrations as m

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "select checksum from migrations where version = 1"
            ).fetchone()
            assert row is not None
            assert row[0] == _checksum(body)
        finally:
            conn.close()


class TestApplyMigrationRace:
    """Direct tests of the under-lock race branch in _apply_migration.

    The concurrency tests exercise the happy path, but losing threads early-out
    before reaching this branch; these drive it deterministically.
    """

    def _seed(self, db_path: Path, version: int, checksum: str) -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(_MIGRATIONS_TABLE_DDL)
        conn.execute(
            "INSERT INTO migrations (version, checksum, applied_at) VALUES (?, ?, ?)",
            (version, checksum, "2026-01-01T00:00:00+00:00"),
        )
        conn.close()

    def test_benign_race_returns_without_reapplying(self, tmp_path: Path) -> None:
        """If the row already exists with a matching checksum, apply is a no-op
        and does NOT run the (would-fail) DDL body."""
        db_path = tmp_path / "test.db"
        sql = "create table t (x integer);"
        cs = _checksum(sql)
        self._seed(db_path, 2, cs)

        # Returns cleanly; never creates 't' because the row already exists.
        _apply_migration(db_path, 2, sql, cs, "0002_t.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name='t'"
                ).fetchone()[0]
                == 0
            )
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_divergent_checksum_race_raises(self, tmp_path: Path) -> None:
        """If the winner recorded a different body for this version, that's a
        real conflict, not a benign race — and the DDL body must never run."""
        db_path = tmp_path / "test.db"
        self._seed(db_path, 2, "0" * 64)
        sql = "create table t (x integer);"

        with pytest.raises(RuntimeError, match="checksum mismatch after race"):
            _apply_migration(db_path, 2, sql, _checksum(sql), "0002_t.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            # The body was never executed: the check happens before any DDL.
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name = 't'"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()


class TestRunMigrations:
    """Integration tests for run_migrations()."""

    def test_applies_initial_schema(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "agents" in tables
            assert "notes" in tables
            assert "todos" in tables
            assert "migrations" in tables
        finally:
            conn.close()

    def test_records_version_and_checksum(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT version, checksum FROM migrations WHERE version = 1"
            ).fetchone()
            assert row is not None
            assert row[0] == 1
            assert len(row[1]) == 64  # SHA-256 hex
        finally:
            conn.close()

    def test_idempotent(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        run_migrations(db_path)
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == len(_discover_migrations())
        finally:
            conn.close()

    def test_checksum_mismatch_raises(self, tmp_path: Path) -> None:
        db_path = tmp_path / "test.db"
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE migrations SET checksum = 'bad' WHERE version = 1")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="checksum mismatch"):
            run_migrations(db_path)

    def test_schema_constraints_work(self, tmp_path: Path) -> None:
        """Verify CHECK constraints are enforced."""
        db_path = tmp_path / "test.db"
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute(
                "INSERT INTO agents (id, name, created_at, updated_at) "
                "VALUES ('a1', 'test', '2026-01-01', '2026-01-01')"
            )
            conn.execute(
                "INSERT INTO notes (id, agent_id, timestamp, category, content) "
                "VALUES ('n1', 'a1', '2026-01-01', 'progress', 'test')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO notes (id, agent_id, timestamp, category, content) "
                    "VALUES ('n2', 'a1', '2026-01-01', 'invalid_cat', 'test')"
                )
        finally:
            conn.close()

    def test_foreign_keys_enforced(self, tmp_path: Path) -> None:
        """Verify FK constraints prevent orphan rows."""
        db_path = tmp_path / "test.db"
        run_migrations(db_path)

        conn = sqlite3.connect(str(db_path), isolation_level=None)
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO notes (id, agent_id, timestamp, category, content) "
                    "VALUES ('n1', 'nonexistent', '2026-01-01', 'note', 'test')"
                )
        finally:
            conn.close()


class TestMultipleMigrations:
    """run_migrations across more than one migration file (the apply loop)."""

    def _write(self, mig_dir: Path, name: str, body: str) -> None:
        (mig_dir / name).write_text(body, encoding="utf-8")

    def test_applies_multiple_pending_in_order(self, tmp_path: Path) -> None:
        """All pending migrations apply in ascending order in a single call.

        0002 inserts into a table created by 0001, so the run only succeeds if
        0001 ran first — proving ordering, not just that all files were seen.
        """
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        self._write(mig_dir, "0001_a.sql", "create table a (x integer primary key);")
        self._write(
            mig_dir,
            "0002_b.sql",
            "create table b (y integer);\ninsert into a (x) values (1);",
        )
        self._write(mig_dir, "0003_c.sql", "create table c (z integer);")
        db_path = tmp_path / "test.db"

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
            assert {"a", "b", "c"}.issubset(tables)
            assert conn.execute("select count(*) from a").fetchone()[0] == 1
            versions = [
                row[0]
                for row in conn.execute(
                    "select version from migrations order by version"
                )
            ]
            assert versions == [1, 2, 3]
        finally:
            conn.close()

    def test_incremental_applies_only_new(self, tmp_path: Path) -> None:
        """Re-running after adding a new migration applies ONLY the new one and
        does not touch the already-applied migration's bookkeeping row."""
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        self._write(mig_dir, "0001_a.sql", "create table a (x integer);")
        db_path = tmp_path / "test.db"

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            first_applied_at = conn.execute(
                "select applied_at from migrations where version = 1"
            ).fetchone()[0]
        finally:
            conn.close()

        # Add a second migration and re-run.
        self._write(mig_dir, "0002_b.sql", "create table b (y integer);")
        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(
                conn.execute("select version, applied_at from migrations").fetchall()
            )
            assert set(rows) == {1, 2}
            # 0001 was NOT re-applied: its applied_at timestamp is unchanged.
            assert rows[1] == first_applied_at
        finally:
            conn.close()

    def test_no_migration_files_is_noop(self, tmp_path: Path) -> None:
        """An empty migrations dir returns cleanly without creating a DB."""
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        db_path = tmp_path / "test.db"

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        assert not db_path.exists()

    def test_missing_applied_file_refuses_to_start(self, tmp_path: Path) -> None:
        """If an already-applied version's file is gone from disk, the runner
        fails loud instead of silently skipping its checksum verification."""
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        self._write(mig_dir, "0001_a.sql", "create table a (x integer);")
        self._write(mig_dir, "0002_b.sql", "create table b (y integer);")
        db_path = tmp_path / "test.db"

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        # Delete an applied migration file, then re-run.
        (mig_dir / "0001_a.sql").unlink()
        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            with pytest.raises(RuntimeError, match="no file on disk"):
                run_migrations(db_path)

    def test_out_of_order_pending_refuses_to_apply(self, tmp_path: Path) -> None:
        """A new file numbered below an already-applied version is rejected,
        rather than applied out of order after a higher version already ran."""
        import np_agent_memory.migrations as m

        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        self._write(mig_dir, "0002_b.sql", "create table b (y integer);")
        db_path = tmp_path / "test.db"

        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            run_migrations(db_path)

        # Introduce a lower-numbered migration after 0002 already applied.
        self._write(mig_dir, "0001_a.sql", "create table a (x integer);")
        with patch.object(m, "_MIGRATIONS_DIR", mig_dir):
            with pytest.raises(RuntimeError, match="out of order"):
                run_migrations(db_path)


class TestConcurrentMigrations:
    """True multi-thread concurrency tests for the migration runner."""

    def test_parallel_migration_runs_no_duplicate_records(self, tmp_path: Path) -> None:
        """Multiple threads calling run_migrations simultaneously should not
        create duplicate migration records or corrupt the schema."""
        db_path = tmp_path / "test.db"
        errors: list[Exception] = []
        n_workers = 4
        # Barrier forces all threads to start the migration at the same instant,
        # maximizing real contention rather than relying on lucky scheduling.
        barrier = threading.Barrier(n_workers)

        def worker() -> None:
            try:
                barrier.wait(timeout=30)
                run_migrations(db_path)
            except Exception as e:
                errors.append(e)

        # Use higher retry budget for this test since fresh connections
        # increase contention surface.
        with patch("np_agent_memory.migrations._MAX_RETRIES", 15):
            threads = [threading.Thread(target=worker) for _ in range(n_workers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
                assert not t.is_alive(), "Thread did not complete within timeout"

        assert errors == [], f"Migration errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == len(_discover_migrations())

            # Schema should be intact
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "agents" in tables
            assert "handovers" in tables
        finally:
            conn.close()

    def test_retry_on_database_locked(self, tmp_path: Path) -> None:
        """Migration retries and succeeds after a lock is released.

        Deterministic: the lock is released by an event the worker triggers
        on its first backoff sleep, and we assert at least one retry happened.
        """
        db_path = tmp_path / "test.db"

        # Pre-create the DB with migrations table so the lock blocks the
        # actual migration application, not table creation.
        # check_same_thread=False: the worker thread releases this lock from
        # within its first backoff (see fake_sleep below).
        blocker = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute(_MIGRATIONS_TABLE_DDL)
        blocker.execute("BEGIN EXCLUSIVE")

        errors: list[Exception] = []
        sleep_calls: list[float] = []
        lock_released = threading.Event()
        real_sleep = time.sleep

        def fake_sleep(delay: float) -> None:
            # First backoff = proof a retry occurred. Release the lock now so
            # the next attempt succeeds, making the test deterministic.
            sleep_calls.append(delay)
            if not lock_released.is_set():
                blocker.execute("ROLLBACK")
                blocker.close()
                lock_released.set()
            real_sleep(0.05)

        def worker() -> None:
            try:
                with patch("np_agent_memory.migrations.time.sleep", fake_sleep):
                    run_migrations(db_path)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive(), "Worker thread did not complete within timeout"

        # A retry must have happened (at least one backoff sleep).
        assert len(sleep_calls) >= 1, "Expected at least one retry/backoff"
        assert errors == [], f"Unexpected errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == len(_discover_migrations())
        finally:
            conn.close()

    def test_bootstrap_retries_on_locked(self, tmp_path: Path) -> None:
        """Contention during the *bootstrap* phase (before the migrations table
        exists) is retried via _retry_on_busy, not just the apply phase.

        A lock is held on a fresh DB so the worker's bootstrap / WAL-conversion
        hits SQLITE_BUSY; the lock is released on the first backoff so the next
        attempt succeeds.
        """
        db_path = tmp_path / "test.db"

        # Fresh DB with NO migrations table — the lock blocks bootstrap itself.
        blocker = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute("CREATE TABLE placeholder (x)")
        blocker.execute("BEGIN EXCLUSIVE")

        errors: list[Exception] = []
        sleep_calls: list[float] = []
        lock_released = threading.Event()
        real_sleep = time.sleep

        def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            if not lock_released.is_set():
                blocker.execute("ROLLBACK")
                blocker.close()
                lock_released.set()
            real_sleep(0.05)

        def worker() -> None:
            try:
                with patch("np_agent_memory.migrations.time.sleep", fake_sleep):
                    run_migrations(db_path)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive(), "Worker thread did not complete within timeout"

        assert len(sleep_calls) >= 1, "Expected at least one retry/backoff"
        assert errors == [], f"Unexpected errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == len(_discover_migrations())
        finally:
            conn.close()

    def test_apply_migration_retries_when_begin_immediate_locked(
        self, tmp_path: Path
    ) -> None:
        """_apply_migration itself (not just bootstrap) retries when another
        connection holds the write lock, then succeeds once it is released."""
        db_path = tmp_path / "test.db"

        # The migrations table already exists, so contention is in the apply
        # phase: _apply_migration's BEGIN IMMEDIATE blocks on the held lock.
        blocker = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute(_MIGRATIONS_TABLE_DDL)
        blocker.execute("BEGIN EXCLUSIVE")

        errors: list[Exception] = []
        sleep_calls: list[float] = []
        lock_released = threading.Event()
        real_sleep = time.sleep

        def fake_sleep(delay: float) -> None:
            sleep_calls.append(delay)
            if not lock_released.is_set():
                blocker.execute("ROLLBACK")
                blocker.close()
                lock_released.set()
            real_sleep(0.05)

        sql = "create table t (x integer);"

        def worker() -> None:
            try:
                with patch("np_agent_memory.migrations.time.sleep", fake_sleep):
                    _apply_migration(db_path, 2, sql, _checksum(sql), "0002_t.sql")
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive(), "Worker thread did not complete within timeout"

        assert len(sleep_calls) >= 1, "Expected at least one retry/backoff"
        assert errors == [], f"Unexpected errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name = 't'"
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()


class TestMigrationErrorPaths:
    """Error path coverage for the migration runner."""

    def test_wal_conversion_error_is_retried(self, tmp_path: Path) -> None:
        """A transient WalConversionError during connection setup is retried by
        the busy loop (cold-start contention) rather than crashing startup."""
        import np_agent_memory.db as db

        db_path = tmp_path / "test.db"
        real_configure = db.configure_connection
        calls = {"n": 0}

        def flaky_configure(conn: sqlite3.Connection) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise db.WalConversionError("transient WAL conversion failure")
            real_configure(conn)

        sleep_calls: list[float] = []
        with (
            patch.object(db, "configure_connection", flaky_configure),
            patch(
                "np_agent_memory.migrations.time.sleep",
                lambda d: sleep_calls.append(d),
            ),
        ):
            run_migrations(db_path)

        assert calls["n"] >= 2, "Expected the first setup to fail and be retried"
        assert len(sleep_calls) >= 1, "Expected at least one retry/backoff"

        conn = sqlite3.connect(str(db_path))
        try:
            assert conn.execute("select count(*) from migrations").fetchone()[0] == len(
                _discover_migrations()
            )
        finally:
            conn.close()

    def test_apply_migration_retries_on_wal_conversion_error(
        self, tmp_path: Path
    ) -> None:
        """_apply_migration's own retry branch (not bootstrap) tolerates a
        transient WalConversionError when opening its connection, then succeeds.

        The migrations table already exists, so bootstrap/read succeed and the
        first failure occurs inside _apply_migration's connection setup."""
        import np_agent_memory.db as db

        db_path = tmp_path / "test.db"
        # Pre-create the migrations table so the failure is isolated to apply.
        seed = sqlite3.connect(str(db_path), isolation_level=None)
        seed.execute("PRAGMA journal_mode = WAL")
        seed.execute(_MIGRATIONS_TABLE_DDL)
        seed.close()

        real_configure = db.configure_connection
        calls = {"n": 0}

        def flaky_configure(conn: sqlite3.Connection) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise db.WalConversionError("transient WAL conversion failure")
            real_configure(conn)

        sleep_calls: list[float] = []
        sql = "create table t (x integer);"
        with (
            patch.object(db, "configure_connection", flaky_configure),
            patch(
                "np_agent_memory.migrations.time.sleep",
                lambda d: sleep_calls.append(d),
            ),
        ):
            _apply_migration(db_path, 2, sql, _checksum(sql), "0002_t.sql")

        assert calls["n"] >= 2, "Expected the first apply-open to fail and retry"
        assert len(sleep_calls) >= 1, "Expected at least one retry/backoff"

        conn = sqlite3.connect(str(db_path))
        try:
            assert (
                conn.execute(
                    "select count(*) from sqlite_master where name = 't'"
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "select count(*) from migrations where version = 2"
                ).fetchone()[0]
                == 1
            )
        finally:
            conn.close()

    def test_is_busy_error_primary_codes(self) -> None:
        """Primary SQLITE_BUSY (5) and SQLITE_LOCKED (6) are classified busy."""
        for code in (5, 6):
            e = sqlite3.OperationalError("x")
            e.sqlite_errorcode = code
            assert _is_busy_error(e) is True

    def test_is_busy_error_extended_codes(self) -> None:
        """Extended codes (e.g. SQLITE_BUSY_SNAPSHOT=517) are masked to primary."""
        for code in (517, 5 + (1 << 8), 6 + (2 << 8)):
            e = sqlite3.OperationalError("x")
            e.sqlite_errorcode = code
            assert _is_busy_error(e) is True, f"code {code} not classified busy"

    def test_is_busy_error_non_busy_code(self) -> None:
        """A non-busy errorcode (e.g. SQLITE_ERROR=1) is not classified busy."""
        e = sqlite3.OperationalError("syntax error")
        e.sqlite_errorcode = 1
        assert _is_busy_error(e) is False

    def test_is_busy_error_substring_fallback(self) -> None:
        """When errorcode is absent, falls back to message substring."""
        # A Python-constructed OperationalError has no sqlite_errorcode set,
        # so getattr(..., None) returns None and the substring path is used.
        e = sqlite3.OperationalError("database is locked")
        assert getattr(e, "sqlite_errorcode", None) is None
        assert _is_busy_error(e) is True
        assert _is_busy_error(sqlite3.OperationalError("no such table")) is False

    def test_max_retries_exhausted_raises(self, tmp_path: Path) -> None:
        """After _MAX_RETRIES, OperationalError should propagate."""
        db_path = tmp_path / "test.db"

        # Pre-create with migrations table
        blocker = sqlite3.connect(str(db_path), isolation_level=None)
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute(_MIGRATIONS_TABLE_DDL)
        blocker.execute("BEGIN EXCLUSIVE")

        try:
            # Use low retries and short delays. The blocker holds the lock
            # throughout, so all retries will fail. busy_timeout=1000 means
            # each attempt waits 1s — keep retries low to keep test fast.
            with patch("np_agent_memory.migrations._MAX_RETRIES", 2):
                with patch("np_agent_memory.migrations._BASE_DELAY_S", 0.01):
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        run_migrations(db_path)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

    def test_failed_migration_rolls_back_atomically(self, tmp_path: Path) -> None:
        """A migration whose later statement fails must leave NO trace:
        no partial tables and no migrations row."""
        db_path = tmp_path / "test.db"
        # First statement is valid, second is invalid SQL → whole txn rolls back.
        bad_sql = (
            "CREATE TABLE good (id INTEGER PRIMARY KEY);\n"
            "CREATE TABLE bad (this is not valid sql);\n"
        )
        cs = _checksum(bad_sql)

        # Bootstrap the real schema + migrations table first.
        run_migrations(db_path)

        with pytest.raises(sqlite3.OperationalError):
            _apply_migration(db_path, 999, bad_sql, cs, "0999_bad.sql")

        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "good" not in tables, "partial table leaked after rollback"
            applied = conn.execute(
                "SELECT COUNT(*) FROM migrations WHERE version = 999"
            ).fetchone()[0]
            assert applied == 0, "failed migration must not be recorded"
        finally:
            conn.close()

    def test_duplicate_migration_versions_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_discover_migrations must reject two files sharing a version."""
        mig_dir = tmp_path / "migrations"
        mig_dir.mkdir()
        (mig_dir / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
        (mig_dir / "0001_b.sql").write_text("SELECT 2;", encoding="utf-8")

        monkeypatch.setattr("np_agent_memory.migrations._MIGRATIONS_DIR", mig_dir)
        with pytest.raises(RuntimeError, match="Duplicate migration version"):
            _discover_migrations()


class TestEnumSchemaMatchesSqlChecks:
    """The Literal-derived runtime tuples are the single source of truth for the
    JSON-schema enums; they MUST stay byte-identical to the SQL CHECK constraints
    so the Python and database validation can never silently diverge.
    """

    @staticmethod
    def _all_check_values(sql: str, column: str) -> list[tuple[str, ...]]:
        """All value sets from every ``check (<column> in (...))`` clause.

        A column name like ``status`` / ``priority`` appears on more than one
        table, so return every match rather than just the first.
        """
        matches = re.findall(
            rf"check\s*\(\s*{re.escape(column)}\s+in\s*\(([^)]*)\)\s*\)",
            sql,
            re.IGNORECASE,
        )
        assert matches, f"no CHECK constraint found for column {column!r}"
        return [
            tuple(value.strip().strip("'") for value in group.split(","))
            for group in matches
        ]

    def test_literals_match_check_constraints(self) -> None:
        from np_agent_memory.tools import blockers, inbox, memory, todos

        init_sql = _discover_migrations()[0][1].read_text(encoding="utf-8")

        assert self._all_check_values(init_sql, "category") == [memory._CATEGORIES]
        # status / priority each appear on multiple tables — assert each Literal
        # is byte-identical to one of the SQL CHECK value sets for that column.
        statuses = self._all_check_values(init_sql, "status")
        assert todos._STATUSES in statuses
        assert blockers._STATUSES in statuses
        priorities = self._all_check_values(init_sql, "priority")
        assert todos._PRIORITIES in priorities
        assert inbox._PRIORITIES in priorities
