"""Tests for the shared tool-layer helpers (tools/_common)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools._common import (
    MAX_LIMIT,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    keyset_predicate,
    require_agent_id,
    resolve_agent_id,
    truncate,
)
from np_agent_memory.tools.agents import register_agent


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


class TestResolveAgentId:
    def test_resolves_registered_agent(
        self, db_conn: sqlite3.Connection, tmp_path: Path
    ) -> None:
        register_agent(db_conn, name="a", agent_cwd=str(tmp_path))
        canonical = db_conn.execute("SELECT alias_path FROM agent_aliases").fetchone()[
            0
        ]
        assert resolve_agent_id(db_conn, canonical) is not None

    def test_returns_none_for_unknown(self, db_conn: sqlite3.Connection) -> None:
        assert resolve_agent_id(db_conn, "c:/nope") is None

    def test_require_raises_for_unknown(self, db_conn: sqlite3.Connection) -> None:
        with pytest.raises(ValueError, match="not registered"):
            require_agent_id(db_conn, "c:/nope")


class TestClampLimit:
    def test_caps_at_max(self) -> None:
        assert clamp_limit(10_000) == MAX_LIMIT

    def test_passes_through_in_range(self) -> None:
        assert clamp_limit(25) == 25

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            clamp_limit(0)

    def test_rejects_bool(self) -> None:
        with pytest.raises(ValueError, match="integer"):
            clamp_limit(True)


class TestCursor:
    def test_round_trips(self) -> None:
        values = ["2026-06-08T12:00:00+00:00", "01ABC"]
        assert decode_cursor(encode_cursor(values)) == values

    def test_rejects_garbage(self) -> None:
        with pytest.raises(ValueError, match="invalid cursor"):
            decode_cursor("!!!not-base64!!!")

    def test_rejects_non_list_payload(self) -> None:
        import base64
        import json

        token = base64.urlsafe_b64encode(json.dumps({"a": 1}).encode()).decode()
        with pytest.raises(ValueError, match="invalid cursor"):
            decode_cursor(token)

    def test_rejects_nested_non_scalar_elements(self) -> None:
        """Regression for review R3: forged cursors with nested arrays/objects
        must raise a clean ValueError, not reach SQLite binding."""
        for payload in ([["nested"]], [{"k": "v"}], ["ok", [1, 2]]):
            token = encode_cursor(payload)
            with pytest.raises(ValueError, match="invalid cursor"):
                decode_cursor(token)

    def test_accepts_scalar_elements(self) -> None:
        for payload in (["s", 1], [2, "t", "id"], [None, "x"], [1.5, "y"]):
            assert decode_cursor(encode_cursor(payload)) == payload


class TestKeysetPredicate:
    def test_single_key(self) -> None:
        sql, params = keyset_predicate([("ts", "T")], direction="<")
        assert sql == "((ts < ?))"
        assert params == ["T"]

    def test_two_keys_descending(self) -> None:
        sql, params = keyset_predicate([("ts", "T"), ("id", "I")], direction="<")
        assert sql == "((ts < ?) OR (ts = ? AND id < ?))"
        assert params == ["T", "T", "I"]

    def test_three_keys(self) -> None:
        sql, params = keyset_predicate(
            [("rank", 2), ("ts", "T"), ("id", "I")], direction="<"
        )
        assert sql == (
            "((rank < ?) OR (rank = ? AND ts < ?) OR (rank = ? AND ts = ? AND id < ?))"
        )
        assert params == [2, 2, "T", 2, "T", "I"]

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="at least one key"):
            keyset_predicate([], direction="<")

    def test_rejects_bad_direction(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            keyset_predicate([("a", 1)], direction="!=")


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert truncate("abc", 10) == ("abc", False)

    def test_long_text_clipped(self) -> None:
        preview, was = truncate("abcdef", 3)
        assert preview == "abc"
        assert was is True
