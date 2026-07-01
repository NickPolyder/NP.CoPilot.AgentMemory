"""Gap 4 — Unicode round-trip tests.

Verifies that multi-byte unicode (emoji, CJK, combining marks) survives the
full save → read cycle for handover body_md / summary, note content, and
metadata dicts.  Pure data fidelity — no truncation involved.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.startup import init_db
from np_agent_memory.tools._common import metadata_to_json
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.handovers import (
    claim_handovers,
    export_handover,
    latest_handover,
    save_handover,
)
from np_agent_memory.tools.memory import log_memory, query_memory


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


@pytest.fixture
def agent_cwd(tmp_path: Path, db_conn: sqlite3.Connection) -> str:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    register_agent(db_conn, name="tester", agent_cwd=str(cwd))
    return str(cwd)


# ---------------------------------------------------------------------------
# Handover unicode round-trips
# ---------------------------------------------------------------------------

_UNICODE_BODY = (
    "# 🚀 Unicode Handover\n\n"
    "## CJK\n中文内容 日本語テスト 한국어\n\n"
    "## Emoji\n🎉🦊🌍🔑\n\n"
    "## Combining marks\n"
    "e\u0301 a\u0300 u\u0308\n"  # é à ü via combining
    "## Symbols\n"
    "→ ← ↑ ↓ • © ®\n"
)

_UNICODE_SUMMARY = "🚀 日本語テスト éàü – session end"

_UNICODE_META = {
    "🔑 key": "value 🔐",
    "中文键": "日本語値",
    "emoji_val": "🚀🎉",
    "combining": "e\u0301",
}


class TestHandoverUnicodeRoundTrip:
    def test_body_md_unicode_roundtrip_via_latest(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Multi-byte body_md bytes must survive save → latest_handover
        byte-for-byte.
        """
        save_handover(
            db_conn, agent_cwd=agent_cwd, summary="unicode test", body_md=_UNICODE_BODY
        )
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=True)
        assert out["handover"]["body_md"] == _UNICODE_BODY

    def test_summary_unicode_roundtrip_via_latest(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Multi-byte summary must survive save → latest_handover byte-for-byte."""
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary=_UNICODE_SUMMARY,
            body_md="plain body",
        )
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=True)
        assert out["handover"]["summary"] == _UNICODE_SUMMARY

    def test_body_md_unicode_roundtrip_via_export(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Multi-byte body_md bytes must survive save → export_handover."""
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary="unicode export",
            body_md=_UNICODE_BODY,
        )
        out = export_handover(db_conn, agent_cwd=agent_cwd)
        # The full body is embedded verbatim in the markdown rendering.
        assert _UNICODE_BODY in out["markdown"]

    def test_metadata_unicode_roundtrip_via_latest(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Unicode keys and values in metadata must survive save → latest_handover."""
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary="meta unicode",
            body_md="b",
            metadata=_UNICODE_META,
        )
        out = latest_handover(db_conn, agent_cwd=agent_cwd, full=True)
        assert out["handover"]["metadata"] == _UNICODE_META

    def test_metadata_unicode_roundtrip_via_claim(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Unicode metadata must survive save → claim_handovers."""
        save_handover(
            db_conn,
            agent_cwd=agent_cwd,
            summary="claim unicode meta",
            body_md="b",
            metadata=_UNICODE_META,
        )
        out = claim_handovers(db_conn, consumer_id="ingest", limit=10)
        assert out["handovers"][0]["metadata"] == _UNICODE_META


# ---------------------------------------------------------------------------
# Note (memory) unicode round-trips
# ---------------------------------------------------------------------------


class TestNoteUnicodeRoundTrip:
    def test_note_content_unicode_roundtrip(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Multi-byte note content must survive log_memory → query_memory."""
        content = (
            "Progress: 🚀 中文 한국어 "
            "e\u0301 a\u0300 u\u0308 "  # combining marks
            "→ ← • © ® ™"
        )
        log_memory(db_conn, agent_cwd=agent_cwd, category="note", content=content)
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert out["notes"][0]["content"] == content

    def test_note_topic_unicode_roundtrip(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Unicode topic must survive log_memory → query_memory."""
        topic = "日本語トピック 🔑"
        log_memory(
            db_conn,
            agent_cwd=agent_cwd,
            category="progress",
            content="c",
            topic=topic,
        )
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert out["notes"][0]["topic"] == topic

    def test_note_metadata_unicode_roundtrip(
        self, db_conn: sqlite3.Connection, agent_cwd: str
    ) -> None:
        """Unicode metadata on a note must survive log_memory → query_memory."""
        meta = {"🔑": "🔐", "中文": "日本語"}
        log_memory(
            db_conn,
            agent_cwd=agent_cwd,
            category="decision",
            content="decided",
            metadata=meta,
        )
        out = query_memory(db_conn, agent_cwd=agent_cwd, limit=1, full=True)
        assert out["notes"][0]["metadata"] == meta


# ---------------------------------------------------------------------------
# metadata_to_json unicode handling (unit — no DB needed)
# ---------------------------------------------------------------------------


class TestMetadataToJsonUnicode:
    def test_unicode_keys_and_values_roundtrip(self) -> None:
        """metadata_to_json must preserve multi-byte unicode keys and values."""
        meta = {
            "🔑 key": "value 🔐",
            "中文键": "日本語値",
            "combining": "e\u0301",
        }
        encoded = metadata_to_json(meta)
        assert encoded is not None
        decoded = json.loads(encoded)
        assert decoded == meta

    def test_emoji_keys_and_values_preserved(self) -> None:
        """Emoji in both keys and values survive JSON round-trip."""
        meta = {"🚀": "🎉", "🦊": "🌍"}
        encoded = metadata_to_json(meta)
        assert encoded is not None
        assert json.loads(encoded) == meta

    def test_cjk_roundtrip(self) -> None:
        """CJK characters in keys and values survive JSON round-trip."""
        meta = {"中文键": "中文值", "日本語": "テスト"}
        encoded = metadata_to_json(meta)
        assert encoded is not None
        assert json.loads(encoded) == meta

    def test_combining_marks_preserved(self) -> None:
        """Unicode combining marks are preserved through JSON serialization."""
        # é can be encoded as U+00E9 (precomposed) or e + U+0301 (combining).
        # Python strings compare equal only if the codepoints match; we test
        # the combining form round-trips faithfully.
        combining = "e\u0301 a\u0300 u\u0308"
        meta = {"key": combining}
        encoded = metadata_to_json(meta)
        assert encoded is not None
        assert json.loads(encoded) == meta
