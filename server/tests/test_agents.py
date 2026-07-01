"""Tests for the Phase 3 agent-identity layer (identity + tools/agents)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.identity import (
    canonicalize_agent_cwd,
    display_basename,
    new_ulid,
    now_iso,
)
from np_agent_memory.startup import init_db
from np_agent_memory.tools import register_all_tools
from np_agent_memory.tools.agents import (
    _MAX_NAME_LEN,
    add_alias,
    describe_agent,
    list_agents,
    register_agent,
    rename_agent,
)


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Yield a connection to a freshly migrated temp database."""
    data_dir = tmp_path / "data"
    db_path = init_db(data_dir)
    with open_connection(db_path) as conn:
        yield conn


def _agent_id_for(conn: sqlite3.Connection, canonical: str) -> str:
    row = conn.execute(
        "SELECT agent_id FROM agent_aliases WHERE alias_path = ?", (canonical,)
    ).fetchone()
    assert row is not None
    return row["agent_id"]


# ---------------------------------------------------------------------------
# canonicalize_agent_cwd
# ---------------------------------------------------------------------------


class TestCanonicalize:
    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            canonicalize_agent_cwd("   ")

    def test_rejects_relative(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            canonicalize_agent_cwd("relative/path")

    def test_rejects_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        with pytest.raises(ValueError, match="does not exist"):
            canonicalize_agent_cwd(str(missing))

    def test_rejects_file(self, tmp_path: Path) -> None:
        f = tmp_path / "afile.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="must be a directory"):
            canonicalize_agent_cwd(str(f))

    def test_uses_forward_slashes(self, tmp_path: Path) -> None:
        assert "\\" not in canonicalize_agent_cwd(str(tmp_path))

    def test_trailing_separator_is_idempotent(self, tmp_path: Path) -> None:
        base = canonicalize_agent_cwd(str(tmp_path))
        with_sep = canonicalize_agent_cwd(str(tmp_path) + "\\")
        assert base == with_sep
        assert not base.endswith("/")

    def test_case_insensitive_on_windows(self, tmp_path: Path) -> None:
        # Windows filesystems are case-insensitive; normcase folds case.
        lower = canonicalize_agent_cwd(str(tmp_path).lower())
        upper = canonicalize_agent_cwd(str(tmp_path).upper())
        assert lower == upper

    def test_anchor_keeps_its_slash(self) -> None:
        # Drive root must not be stripped to a bare drive letter.
        anchor = Path.cwd().anchor  # e.g. "C:\\"
        canon = canonicalize_agent_cwd(anchor)
        assert canon.endswith("/")
        assert canon.endswith(":/")

    def test_rejects_overly_long_path(self) -> None:
        # Absolute-looking but pathologically long input is rejected up front.
        too_long = "C:\\" + ("a" * 5000)
        with pytest.raises(ValueError, match="too long"):
            canonicalize_agent_cwd(too_long)

    def test_symlink_resolves_to_target(self, tmp_path: Path) -> None:
        # The identity invariant: a symlink and its target collapse to one
        # canonical path (so they resolve to the same agent).
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation not permitted: {exc}")
        assert canonicalize_agent_cwd(str(link)) == canonicalize_agent_cwd(str(target))

    def test_lookup_mode_allows_missing_path(self, tmp_path: Path) -> None:
        # require_exists=False canonicalizes a stored alias key even if the
        # path no longer exists (move/rename recovery), and matches the strict
        # canonical form produced while the path still existed.
        repo = tmp_path / "repo"
        repo.mkdir()
        strict = canonicalize_agent_cwd(str(repo))
        repo.rmdir()
        lenient = canonicalize_agent_cwd(str(repo), require_exists=False)
        assert lenient == strict


class TestUlidAndTimestamp:
    def test_new_ulid_unique_and_sized(self) -> None:
        a, b = new_ulid(), new_ulid()
        assert a != b
        assert len(a) == 26

    def test_now_iso_has_offset(self) -> None:
        assert "+00:00" in now_iso()

    def test_now_iso_is_parseable_utc(self) -> None:
        from datetime import UTC, datetime

        parsed = datetime.fromisoformat(now_iso())
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == UTC.utcoffset(None)


class TestDisplayBasename:
    def test_preserves_on_disk_casing(self, tmp_path: Path) -> None:
        repo = tmp_path / "MixedCase.Repo"
        repo.mkdir()
        assert display_basename(str(repo)) == "MixedCase.Repo"

    def test_falls_back_to_agent_for_drive_root(self) -> None:
        anchor = Path(os.getcwd()).anchor  # e.g. "C:\\" or "/"
        assert display_basename(anchor) == "agent"


# ---------------------------------------------------------------------------
# register_agent
# ---------------------------------------------------------------------------


class TestRegisterAgent:
    def test_first_registration_is_new(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = register_agent(
            db_conn, name="backend", agent_cwd=str(tmp_path), workstream="np"
        )
        assert result["registered"] == "new"
        assert result["name"] == "backend"
        assert result["workstream"] == "np"
        assert "\\" not in result["canonical_path"]

    def test_no_internal_id_leaked(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = register_agent(db_conn, name="backend", agent_cwd=str(tmp_path))
        assert "id" not in result
        assert "agent_id" not in result

    def test_repeat_registration_is_existing_and_keeps_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # Name is sticky: re-registering with a different name must NOT change
        # the stored name — a passed name is silently ignored for an existing
        # agent. Renames go through rename_agent instead.
        register_agent(db_conn, name="old", agent_cwd=str(tmp_path))
        result = register_agent(db_conn, name="new-name", agent_cwd=str(tmp_path))
        assert result["registered"] == "existing"
        assert result["name"] == "old"

    def test_existing_agent_ignores_invalid_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # Sticky-name contract: re-registering an existing agent ignores the
        # passed name entirely, so a blank/overlong name must NOT raise — it
        # must be silently dropped and the stored name preserved.
        register_agent(db_conn, name="original", agent_cwd=str(tmp_path))
        blank = register_agent(db_conn, name="   ", agent_cwd=str(tmp_path))
        assert blank["registered"] == "existing"
        assert blank["name"] == "original"
        overlong = register_agent(db_conn, name="x" * 200, agent_cwd=str(tmp_path))
        assert overlong["name"] == "original"

    def test_omitted_workstream_does_not_erase(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path), workstream="np")
        result = register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        assert result["workstream"] == "np"

    def test_description_is_settable_and_preserved(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(
            db_conn, name="a", agent_cwd=str(tmp_path), description="does things"
        )
        result = register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        assert result["description"] == "does things"

    def test_one_agent_per_canonical_path(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path) + "\\")
        count = db_conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
        assert count == 1

    def test_blank_name_rejected(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # A *provided* name must be non-blank: a whitespace-only string would
        # clobber a good label. (Omitting name entirely is allowed — it
        # defaults on create and preserves on update; covered separately.)
        with pytest.raises(ValueError, match="non-empty"):
            register_agent(db_conn, name="   ", agent_cwd=str(tmp_path))

    def test_omitted_name_defaults_to_directory_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        repo = tmp_path / "MyCoolRepo"
        repo.mkdir()
        result = register_agent(db_conn, agent_cwd=str(repo))
        assert result["registered"] == "new"
        assert result["name"] == "MyCoolRepo"

    def test_omitted_name_on_reregister_preserves_custom_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="custom-name", agent_cwd=str(tmp_path))
        result = register_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["registered"] == "existing"
        assert result["name"] == "custom-name"

    def test_overly_long_name_rejected(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="name is too long"):
            register_agent(db_conn, name="x" * 200, agent_cwd=str(tmp_path))

    def test_overly_long_description_rejected(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="description is too long"):
            register_agent(
                db_conn, name="a", agent_cwd=str(tmp_path), description="x" * 5000
            )

    def test_reregister_preserves_created_at(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        first = register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        created = first["created_at"]
        second = register_agent(db_conn, name="b", agent_cwd=str(tmp_path))
        assert second["registered"] == "existing"
        assert second["created_at"] == created

    def test_concurrent_first_registration_converges_to_one_agent(
        self, tmp_path: Path
    ) -> None:
        # The multi-process invariant: a registration that loses the
        # BEGIN IMMEDIATE race retries, re-reads the alias the winner inserted,
        # and converges to ONE agent instead of minting a second ULID or hitting
        # an unretried alias-PK violation. Simulated single-process: a peer holds
        # the write lock with the agent+alias staged; the loser's retry/backoff
        # sleep commits the peer, then the loser's retry succeeds and converges.
        data_dir = tmp_path / "data"
        db_path = init_db(data_dir)
        repo = tmp_path / "repo"
        repo.mkdir()
        canonical = canonicalize_agent_cwd(str(repo))

        with open_connection(db_path) as peer, open_connection(db_path) as main:
            ts = now_iso()
            agent_id = new_ulid()
            peer.execute("BEGIN IMMEDIATE")
            peer.execute(
                "INSERT INTO agents "
                "(id, name, workstream, description, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent_id, "winner", None, None, ts, ts),
            )
            peer.execute(
                "INSERT INTO agent_aliases (alias_path, agent_id, created_at) "
                "VALUES (?, ?, ?)",
                (canonical, agent_id, ts),
            )

            # Fail the loser's BEGIN IMMEDIATE fast so the retry loop engages.
            main.execute("PRAGMA busy_timeout = 50")

            committed = {"done": False}

            def releasing_sleep(_delay: float) -> None:
                if not committed["done"]:
                    peer.execute("COMMIT")
                    committed["done"] = True

            with patch("np_agent_memory.db.time.sleep", releasing_sleep):
                result = register_agent(main, name="loser", agent_cwd=str(repo))

        assert committed["done"], "the loser never retried (no race exercised)"
        assert result["registered"] == "existing"
        assert result["name"] == "winner"  # sticky name: the loser sees "existing"
        with open_connection(db_path) as check:
            assert check.execute("SELECT COUNT(*) FROM agents").fetchone()[0] == 1
            assert (
                check.execute("SELECT COUNT(*) FROM agent_aliases").fetchone()[0] == 1
            )


# ---------------------------------------------------------------------------
# describe_agent
# ---------------------------------------------------------------------------


class TestDescribeAgent:
    def test_unregistered_is_soft_false(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["registered"] is False
        assert "hint" in result

    def test_registered_returns_zero_counts(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["registered"] is True
        assert result["unread_messages"] == 0
        assert result["open_todos"] == 0
        assert result["active_blockers"] == 0

    def test_counts_reflect_open_work(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        canonical = canonicalize_agent_cwd(str(tmp_path))
        agent_id = _agent_id_for(db_conn, canonical)
        ts = now_iso()

        # Open todo (counted) + done todo (not counted).
        db_conn.execute(
            "INSERT INTO todos (id, agent_id, title, status, priority, "
            "created_at, updated_at) VALUES (?, ?, 'open', 'pending', 'normal', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )
        db_conn.execute(
            "INSERT INTO todos (id, agent_id, title, status, priority, "
            "created_at, updated_at) VALUES (?, ?, 'closed', 'done', 'normal', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )
        # Active blocker (counted) + resolved blocker (not counted).
        db_conn.execute(
            "INSERT INTO blockers (id, agent_id, title, status, raised_at) "
            "VALUES (?, ?, 'b1', 'active', ?)",
            (new_ulid(), agent_id, ts),
        )
        db_conn.execute(
            "INSERT INTO blockers (id, agent_id, title, status, raised_at) "
            "VALUES (?, ?, 'b2', 'resolved', ?)",
            (new_ulid(), agent_id, ts),
        )
        # Unread inbox message (counted) + read message (not counted).
        db_conn.execute(
            "INSERT INTO inbox (id, to_agent_id, subject, body, sent_at) "
            "VALUES (?, ?, 's', 'b', ?)",
            (new_ulid(), agent_id, ts),
        )
        db_conn.execute(
            "INSERT INTO inbox (id, to_agent_id, subject, body, sent_at, read_at) "
            "VALUES (?, ?, 's', 'b', ?, ?)",
            (new_ulid(), agent_id, ts, ts),
        )

        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["open_todos"] == 1
        assert result["active_blockers"] == 1
        assert result["unread_messages"] == 1

    def test_registered_returns_metadata_fields(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(
            db_conn,
            name="a",
            agent_cwd=str(tmp_path),
            workstream="np",
            description="role",
        )
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["workstream"] == "np"
        assert result["description"] == "role"
        assert "\\" not in result["canonical_path"]
        assert "id" not in result
        assert "agent_id" not in result

    @pytest.mark.parametrize(
        ("status", "counted"),
        [
            ("pending", True),
            ("in_progress", True),
            ("blocked", True),
            ("done", False),
            ("cancelled", False),
        ],
    )
    def test_open_todo_status_filter_is_exhaustive(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        status: str,
        counted: bool,
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        agent_id = _agent_id_for(db_conn, canonicalize_agent_cwd(str(tmp_path)))
        ts = now_iso()
        db_conn.execute(
            "INSERT INTO todos (id, agent_id, title, status, priority, "
            "created_at, updated_at) VALUES (?, ?, 't', ?, 'normal', ?, ?)",
            (new_ulid(), agent_id, status, ts, ts),
        )
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["open_todos"] == (1 if counted else 0)

    @pytest.mark.parametrize(
        ("status", "counted"),
        [
            ("active", True),
            ("escalated", True),
            ("resolved", False),
        ],
    )
    def test_active_blocker_status_filter(
        self,
        db_conn: sqlite3.Connection,
        tmp_path: Path,
        status: str,
        counted: bool,
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        agent_id = _agent_id_for(db_conn, canonicalize_agent_cwd(str(tmp_path)))
        ts = now_iso()
        db_conn.execute(
            "INSERT INTO blockers (id, agent_id, title, status, raised_at) "
            "VALUES (?, ?, 'b', ?, ?)",
            (new_ulid(), agent_id, status, ts),
        )
        result = describe_agent(db_conn, agent_cwd=str(tmp_path))
        assert result["active_blockers"] == (1 if counted else 0)


# ---------------------------------------------------------------------------
# add_alias
# ---------------------------------------------------------------------------


class TestRenameAgent:
    def test_rename_changes_stored_name(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="old", agent_cwd=str(tmp_path))
        result = rename_agent(db_conn, agent_cwd=str(tmp_path), name="new")
        assert result["renamed"] is True
        assert result["name"] == "new"
        assert describe_agent(db_conn, agent_cwd=str(tmp_path))["name"] == "new"

    def test_rename_persists_over_reregister(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # After an explicit rename, a subsequent register (even with a name)
        # must keep the renamed value — register never fights a rename.
        register_agent(db_conn, name="old", agent_cwd=str(tmp_path))
        rename_agent(db_conn, agent_cwd=str(tmp_path), name="chosen")
        result = register_agent(db_conn, name="drifted", agent_cwd=str(tmp_path))
        assert result["name"] == "chosen"

    def test_rename_preserves_created_at_and_metadata(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        first = register_agent(
            db_conn,
            name="old",
            agent_cwd=str(tmp_path),
            workstream="np",
            description="does things",
        )
        result = rename_agent(db_conn, agent_cwd=str(tmp_path), name="new")
        assert result["created_at"] == first["created_at"]
        assert result["workstream"] == "np"
        assert result["description"] == "does things"

    def test_rename_unregistered_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        with pytest.raises(ValueError, match="not registered"):
            rename_agent(db_conn, agent_cwd=str(tmp_path), name="new")

    def test_rename_blank_name_rejected(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="old", agent_cwd=str(tmp_path))
        with pytest.raises(ValueError):
            rename_agent(db_conn, agent_cwd=str(tmp_path), name="   ")

    def test_rename_resolves_via_alias(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        primary = tmp_path / "primary"
        secondary = tmp_path / "secondary"
        primary.mkdir()
        secondary.mkdir()
        register_agent(db_conn, name="old", agent_cwd=str(primary))
        add_alias(db_conn, agent_cwd=str(primary), new_cwd=str(secondary))
        rename_agent(db_conn, agent_cwd=str(secondary), name="renamed")
        assert describe_agent(db_conn, agent_cwd=str(primary))["name"] == "renamed"

    def test_rename_overlong_name_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Name longer than _MAX_NAME_LEN (128) must raise with 'too long'."""
        register_agent(db_conn, name="original", agent_cwd=str(tmp_path))
        too_long = "x" * (_MAX_NAME_LEN + 1)
        with pytest.raises(ValueError, match="too long"):
            rename_agent(db_conn, agent_cwd=str(tmp_path), name=too_long)

    def test_rename_whitespace_only_name_raises_with_message(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Whitespace-only name must raise with 'name is required'."""
        register_agent(db_conn, name="original", agent_cwd=str(tmp_path))
        with pytest.raises(ValueError, match="name is required"):
            rename_agent(db_conn, agent_cwd=str(tmp_path), name="   ")


class TestAddAlias:
    def test_adds_and_resolves_to_same_agent(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        primary = tmp_path / "primary"
        secondary = tmp_path / "secondary"
        primary.mkdir()
        secondary.mkdir()

        register_agent(db_conn, name="a", agent_cwd=str(primary))
        result = add_alias(db_conn, agent_cwd=str(primary), new_cwd=str(secondary))
        assert result["added"] is True

        via_alias = describe_agent(db_conn, agent_cwd=str(secondary))
        assert via_alias["registered"] is True
        assert via_alias["name"] == "a"

    def test_idempotent_no_op(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        secondary = tmp_path / "secondary"
        secondary.mkdir()
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))
        again = add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))
        assert again["added"] is False

    def test_unregistered_source_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        secondary = tmp_path / "secondary"
        secondary.mkdir()
        with pytest.raises(ValueError, match="not registered"):
            add_alias(db_conn, agent_cwd=str(tmp_path), new_cwd=str(secondary))

    def test_conflict_with_different_agent_raises(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        path_a = tmp_path / "a"
        path_b = tmp_path / "b"
        path_a.mkdir()
        path_b.mkdir()
        register_agent(db_conn, name="agent-a", agent_cwd=str(path_a))
        register_agent(db_conn, name="agent-b", agent_cwd=str(path_b))
        with pytest.raises(ValueError, match="different agent"):
            add_alias(db_conn, agent_cwd=str(path_a), new_cwd=str(path_b))

    def test_moved_source_path_still_resolves(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # Move/rename recovery: the old registered path no longer exists on
        # disk, but it can still be used as the add_alias source to attach the
        # new path to the same agent (source canonicalized in lookup mode).
        old = tmp_path / "old"
        new = tmp_path / "new"
        old.mkdir()
        new.mkdir()
        register_agent(db_conn, name="a", agent_cwd=str(old))
        old.rmdir()  # old path is gone after the move

        result = add_alias(db_conn, agent_cwd=str(old), new_cwd=str(new))
        assert result["added"] is True

        via_new = describe_agent(db_conn, agent_cwd=str(new))
        assert via_new["registered"] is True
        assert via_new["name"] == "a"


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


class TestListAgents:
    def _register(
        self,
        conn: sqlite3.Connection,
        tmp_path: Path,
        name: str,
        **kwargs: object,
    ) -> str:
        repo = tmp_path / name
        repo.mkdir()
        register_agent(conn, name=name, agent_cwd=str(repo), **kwargs)
        return str(repo)

    def test_empty_directory(self, db_conn: sqlite3.Connection) -> None:
        result = list_agents(db_conn, limit=20)
        assert result == {"agents": [], "count": 0, "next_cursor": None}

    def test_lists_all_registered_agents(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._register(db_conn, tmp_path, "a")
        self._register(db_conn, tmp_path, "b")
        self._register(db_conn, tmp_path, "c")

        result = list_agents(db_conn, limit=20)
        assert result["count"] == 3
        assert {a["name"] for a in result["agents"]} == {"a", "b", "c"}
        assert result["next_cursor"] is None

    def test_no_internal_id_leaked(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._register(db_conn, tmp_path, "a")
        agent = list_agents(db_conn, limit=20)["agents"][0]
        assert "id" not in agent
        assert "agent_id" not in agent
        assert "\\" not in agent["canonical_path"]

    def test_cursor_does_not_leak_internal_ulid(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        # The opaque next_cursor is only base64-encoded, not encrypted. The hard
        # rule "agents never see internal IDs" must hold for the cursor too, so
        # its decoded ordering key must carry no agents.id ULID.
        from np_agent_memory.tools._common import decode_cursor

        ids: set[str] = set()
        for name in ("a", "b", "c"):
            path = self._register(db_conn, tmp_path, name)
            ids.add(_agent_id_for(db_conn, canonicalize_agent_cwd(path)))

        page = list_agents(db_conn, limit=2)
        assert page["next_cursor"] is not None
        key = decode_cursor(page["next_cursor"])
        assert len(key) == 2  # (created_at, canonical_path)
        assert not (set(map(str, key)) & ids)

    def test_newest_first_ordering(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        with patch(
            "np_agent_memory.tools.agents.now_iso",
            side_effect=[
                "2026-01-01T00:00:00+00:00",  # a
                "2026-01-02T00:00:00+00:00",  # b
                "2026-01-03T00:00:00+00:00",  # c
            ],
        ):
            self._register(db_conn, tmp_path, "a")
            self._register(db_conn, tmp_path, "b")
            self._register(db_conn, tmp_path, "c")

        names = [a["name"] for a in list_agents(db_conn, limit=20)["agents"]]
        assert names == ["c", "b", "a"]

    def test_pagination_covers_all_without_duplicates(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        for name in ("a", "b", "c", "d", "e"):
            self._register(db_conn, tmp_path, name)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(10):  # generous bound; should finish in 3 pages
            page = list_agents(db_conn, limit=2, cursor=cursor)
            assert len(page["agents"]) <= 2
            seen.extend(a["name"] for a in page["agents"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert cursor is None
        assert sorted(seen) == ["a", "b", "c", "d", "e"]
        assert len(seen) == len(set(seen))

    def test_limit_is_capped(self, db_conn: sqlite3.Connection, tmp_path: Path) -> None:
        from np_agent_memory.tools._common import MAX_LIMIT

        with patch("np_agent_memory.tools.agents.clamp_limit") as clamp:
            clamp.return_value = MAX_LIMIT
            list_agents(db_conn, limit=10_000)
        clamp.assert_called_once_with(10_000)

    def test_rejects_non_positive_limit(self, db_conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="limit must be"):
            list_agents(db_conn, limit=0)

    def test_workstream_filter_is_exact_match(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._register(db_conn, tmp_path, "a", workstream="np")
        self._register(db_conn, tmp_path, "b", workstream="np")
        self._register(db_conn, tmp_path, "c", workstream="other")

        result = list_agents(db_conn, limit=20, workstream="np")
        assert {a["name"] for a in result["agents"]} == {"a", "b"}

    def test_description_truncated_by_default(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        long = "x" * 400
        self._register(db_conn, tmp_path, "a", description=long)

        agent = list_agents(db_conn, limit=20)["agents"][0]
        assert agent["description_truncated"] is True
        assert len(agent["description"]) == 280

    def test_full_returns_untruncated_description(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        long = "x" * 400
        self._register(db_conn, tmp_path, "a", description=long)

        agent = list_agents(db_conn, limit=20, full=True)["agents"][0]
        assert agent["description_truncated"] is False
        assert agent["description"] == long

    def test_null_description_is_not_flagged_truncated(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        self._register(db_conn, tmp_path, "a")
        agent = list_agents(db_conn, limit=20)["agents"][0]
        assert agent["description"] is None
        assert agent["description_truncated"] is False

    def test_invalid_cursor_raises(self, db_conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            list_agents(db_conn, limit=20, cursor="!!!not-base64!!!")


# ---------------------------------------------------------------------------
# tool registration wiring
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_registers_expected_tools(self) -> None:
        from mcp.server.fastmcp import FastMCP

        probe = FastMCP(name="probe")
        register_all_tools(probe)
        import anyio

        tools = anyio.run(probe.list_tools)
        names = {t.name for t in tools}
        assert {
            "agent_register",
            "agent_describe",
            "agent_rename",
            "agent_add_alias",
            "agent_list",
        } <= names
        assert {
            "memory_log",
            "memory_query",
            "memory_export",
            "memory_delete",
            "memory_restore",
        } <= names
        assert {"todo_add", "todo_list", "todo_update"} <= names
        assert {
            "blocker_open",
            "blocker_list",
            "blocker_resolve",
            "blocker_escalate",
        } <= names
        assert {
            "handover_save",
            "handover_latest",
            "handover_export",
            "handover_claim",
            "handover_ack",
            "handover_release",
            "handover_quarantined",
        } <= names
        assert {"inbox_send", "inbox_check", "inbox_ack"} <= names
        assert "memory_backup_now" in names

    def test_enum_params_publish_json_schema_enums_and_descriptions(self) -> None:
        """Enum/confusable params must reach the client schema so agents pick
        valid values (and the right field) on the *first* call — not by failing
        and reading the error. Regression for the "first call always fails" bug.
        """
        from mcp.server.fastmcp import FastMCP

        probe = FastMCP(name="probe")
        register_all_tools(probe)
        import anyio

        schemas = {t.name: t.inputSchema for t in anyio.run(probe.list_tools)}

        def prop(tool: str, name: str) -> dict:
            return schemas[tool]["properties"][name]

        def enum_of(tool: str, name: str) -> list | None:
            p = prop(tool, name)
            if "enum" in p:
                return p["enum"]
            # optional (nullable) params surface the enum inside an anyOf branch.
            for branch in p.get("anyOf", []):
                if "enum" in branch:
                    return branch["enum"]
            return None

        # Required enum is published so the model cannot guess an invalid value.
        assert enum_of("memory_log", "category") == ["progress", "decision", "note"]
        assert enum_of("memory_query", "category") == ["progress", "decision", "note"]
        assert enum_of("todo_add", "priority") == ["low", "normal", "high", "urgent"]
        assert enum_of("todo_list", "status") == [
            "pending",
            "in_progress",
            "done",
            "blocked",
            "cancelled",
        ]
        assert enum_of("todo_list", "sort") == ["recent", "priority"]
        assert enum_of("blocker_list", "status") == ["active", "escalated", "resolved"]
        assert enum_of("inbox_send", "priority") == ["low", "normal", "high", "urgent"]
        assert enum_of("inbox_ack", "status") == ["read", "acked"]

        # Confusable required text fields carry a description so agents send the
        # right field (content vs summary vs body_md) the first time.
        assert prop("memory_log", "content").get("description")
        assert prop("handover_save", "summary").get("description")
        assert prop("handover_save", "body_md").get("description")
        assert schemas["handover_save"]["required"] == [
            "agent_cwd",
            "summary",
            "body_md",
        ]

    def test_new_tools_advertise_params_in_schema(self) -> None:
        """blocker_escalate, handover_quarantined, and handover_claim must expose
        their key parameters in the JSON inputSchema so agents can use them
        correctly on the first call.
        """
        from mcp.server.fastmcp import FastMCP

        probe = FastMCP(name="probe")
        register_all_tools(probe)
        import anyio

        schemas = {t.name: t.inputSchema for t in anyio.run(probe.list_tools)}

        def props(tool: str) -> dict:
            return schemas[tool]["properties"]

        # blocker_escalate advertises its optional reason param with a description
        assert "reason" in props("blocker_escalate")
        assert props("blocker_escalate")["reason"].get("description")

        # handover_quarantined advertises limit / cursor / full
        assert "limit" in props("handover_quarantined")
        assert "cursor" in props("handover_quarantined")
        assert "full" in props("handover_quarantined")

        # handover_claim advertises full as a boolean param
        assert "full" in props("handover_claim")
        full_prop = props("handover_claim")["full"]
        # FastMCP maps bool to "boolean" type; check the type or anyOf branches.
        schema_types = {full_prop.get("type")} | {
            b.get("type") for b in full_prop.get("anyOf", [])
        }
        assert "boolean" in schema_types
