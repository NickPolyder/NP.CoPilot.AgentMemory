"""Tests for np_agent_memory.migrations — migration runner correctness."""

from __future__ import annotations

import hashlib
import sqlite3
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from np_agent_memory.migrations import (
    _checksum,
    _discover_migrations,
    _MIGRATIONS_DIR,
    _split_statements,
    _strip_leading_comments,
    run_migrations,
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


class TestSplitStatements:
    """Tests for _split_statements() SQL splitting."""

    def test_splits_multiple_statements(self) -> None:
        sql = "CREATE TABLE a (x INT);\nCREATE TABLE b (y INT);"
        stmts = _split_statements(sql)
        assert len(stmts) == 2
        assert "CREATE TABLE a" in stmts[0]
        assert "CREATE TABLE b" in stmts[1]

    def test_skips_blank_entries(self) -> None:
        sql = "SELECT 1;;\n;\nSELECT 2;"
        stmts = _split_statements(sql)
        assert len(stmts) == 2

    def test_skips_comment_only_entries(self) -> None:
        sql = "-- just a comment;\nSELECT 1;"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert "SELECT 1" in stmts[0]

    def test_preserves_statement_after_leading_comment(self) -> None:
        """Regression: comments preceding DDL must not drop the statement."""
        sql = textwrap.dedent("""\
            -- Agents table
            CREATE TABLE agents (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL
            );

            -- Notes table
            CREATE TABLE notes (
              id TEXT PRIMARY KEY,
              content TEXT NOT NULL
            );
        """)
        stmts = _split_statements(sql)
        assert len(stmts) == 2
        assert "CREATE TABLE agents" in stmts[0]
        assert "CREATE TABLE notes" in stmts[1]

    def test_preserves_multiline_comment_block_before_statement(self) -> None:
        """Multiple comment lines before a statement are stripped."""
        sql = "-- line 1\n-- line 2\n-- line 3\nCREATE TABLE t (x INT);"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert "CREATE TABLE t" in stmts[0]

    def test_inline_comment_within_statement_preserved(self) -> None:
        """Comments between SQL lines within a statement are kept."""
        sql = "CREATE TABLE t (\n  -- primary key\n  id INT PRIMARY KEY\n);"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert "-- primary key" in stmts[0]

    def test_semicolon_inside_string_literal(self) -> None:
        """Semicolons within single-quoted strings must not split."""
        sql = "INSERT INTO t VALUES ('hello; world', 'foo;bar');"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert "'hello; world'" in stmts[0]
        assert "'foo;bar'" in stmts[0]

    def test_escaped_quotes_in_string(self) -> None:
        """SQLite '' escape within a string doesn't break quote tracking."""
        sql = "INSERT INTO t VALUES ('it''s a test; really');"
        stmts = _split_statements(sql)
        assert len(stmts) == 1
        assert "it''s a test; really" in stmts[0]

    def test_empty_string(self) -> None:
        assert _split_statements("") == []

    def test_only_comments(self) -> None:
        assert _split_statements("-- just a comment\n-- another") == []

    def test_real_init_migration_splits_all_tables(self) -> None:
        """The actual 0001_init.sql produces statements for all tables + indexes."""
        sql = (_MIGRATIONS_DIR / "0001_init.sql").read_text(encoding="utf-8")
        stmts = _split_statements(sql)
        table_stmts = [s for s in stmts if s.strip().lower().startswith("create table")]
        index_stmts = [s for s in stmts if s.strip().lower().startswith("create index")]
        # 8 tables: agents, agent_aliases, notes, todos, blockers, inbox, handovers, backup_runs
        assert len(table_stmts) == 8
        # At least 12 indexes
        assert len(index_stmts) >= 12


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
            assert count == 1
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


class TestConcurrentMigrations:
    """True multi-thread concurrency tests for the migration runner."""

    def test_parallel_migration_runs_no_duplicate_records(self, tmp_path: Path) -> None:
        """Multiple threads calling run_migrations simultaneously should not
        create duplicate migration records or corrupt the schema."""
        db_path = tmp_path / "test.db"
        errors: list[Exception] = []

        def worker() -> None:
            try:
                run_migrations(db_path)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == [], f"Migration errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == 1

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
        """Migration retries and succeeds after a lock is released."""
        db_path = tmp_path / "test.db"

        # Pre-create the DB with migrations table so the lock blocks the
        # actual migration application, not table creation
        blocker = sqlite3.connect(str(db_path), isolation_level=None)
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute(
            "CREATE TABLE IF NOT EXISTS migrations ("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )

        # Hold an exclusive lock
        blocker.execute("BEGIN EXCLUSIVE")

        errors: list[Exception] = []

        def worker() -> None:
            try:
                run_migrations(db_path)
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=worker)
        t.start()
        # Let the worker hit the lock and start retrying
        time.sleep(0.3)
        # Release the lock
        blocker.execute("ROLLBACK")
        blocker.close()
        t.join(timeout=30)

        # Migration should have succeeded after retry
        assert errors == [], f"Unexpected errors: {errors}"

        conn = sqlite3.connect(str(db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM migrations").fetchone()[0]
            assert count == 1
        finally:
            conn.close()


class TestMigrationErrorPaths:
    """Error path coverage for the migration runner."""

    def test_max_retries_exhausted_raises(self, tmp_path: Path) -> None:
        """After _MAX_RETRIES, OperationalError should propagate."""
        db_path = tmp_path / "test.db"

        # Pre-create with migrations table
        blocker = sqlite3.connect(str(db_path), isolation_level=None)
        blocker.execute("PRAGMA journal_mode = WAL")
        blocker.execute(
            "CREATE TABLE IF NOT EXISTS migrations ("
            "version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        blocker.execute("BEGIN EXCLUSIVE")

        try:
            with patch("np_agent_memory.migrations._MAX_RETRIES", 2):
                with patch("np_agent_memory.migrations._BASE_DELAY_S", 0.01):
                    with pytest.raises(sqlite3.OperationalError, match="locked"):
                        run_migrations(db_path)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()

