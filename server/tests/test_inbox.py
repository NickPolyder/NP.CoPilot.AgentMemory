"""Tests for the Phase 6 inbox tools (tools/inbox)."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from np_agent_memory.db import open_connection
from np_agent_memory.identity import canonicalize_agent_cwd
from np_agent_memory.startup import init_db
from np_agent_memory.tools.agents import register_agent
from np_agent_memory.tools.inbox import inbox_ack, inbox_check, inbox_send


@pytest.fixture
def db_conn(tmp_path: Path) -> Generator[sqlite3.Connection, None, None]:
    db_path = init_db(tmp_path / "data")
    with open_connection(db_path) as conn:
        yield conn


def _register(
    conn: sqlite3.Connection,
    tmp_path: Path,
    dirname: str,
    name: str,
) -> dict[str, str]:
    cwd = tmp_path / dirname
    cwd.mkdir()
    result = register_agent(conn, name=name, agent_cwd=str(cwd))
    return {"cwd": str(cwd), "canonical": result["canonical_path"], "name": name}


def _agent_id_for(conn: sqlite3.Connection, cwd: str) -> str:
    canonical = canonicalize_agent_cwd(cwd)
    row = conn.execute(
        "select agent_id from agent_aliases where alias_path = ?",
        (canonical,),
    ).fetchone()
    assert row is not None
    return row["agent_id"]


def test_send_and_receive_happy_path_by_name(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")

    sent = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="hello",
        body="body",
        priority="high",
        metadata={"k": "v", "n": 1},
    )

    assert sent["id"]
    assert sent["to_agent_id"] == _agent_id_for(db_conn, recipient["cwd"])
    assert sent["priority"] == "high"

    out = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=10)
    assert out["count"] == 1
    assert out["next_cursor"] is None
    msg = out["messages"][0]
    assert msg["id"] == sent["id"]
    assert msg["from_agent_id"] == _agent_id_for(db_conn, sender["cwd"])
    assert msg["from_label"] == "alice"
    assert msg["subject"] == "hello"
    assert msg["body"] == "body"
    assert msg["priority"] == "high"
    assert msg["read_at"] is None
    assert msg["acked_at"] is None
    assert msg["metadata"] == {"k": "v", "n": 1}


def test_addressing_by_canonical_path(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")

    sent = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to=recipient["canonical"],
        subject="path",
        body="sent by path",
    )

    out = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=10)
    assert [msg["id"] for msg in out["messages"]] == [sent["id"]]


def test_ambiguous_duplicate_name_raises(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    _register(db_conn, tmp_path, "dup-a", "dup")
    _register(db_conn, tmp_path, "dup-b", "dup")

    with pytest.raises(ValueError, match="matches multiple agents"):
        inbox_send(
            db_conn,
            agent_cwd=sender["cwd"],
            to="dup",
            subject="hello",
            body="body",
        )


def test_unknown_recipient_raises(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")

    with pytest.raises(ValueError, match="recipient not found"):
        inbox_send(
            db_conn,
            agent_cwd=sender["cwd"],
            to="missing",
            subject="hello",
            body="body",
        )


def test_unregistered_sender_raises(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _register(db_conn, tmp_path, "recipient", "bob")
    unregistered = tmp_path / "unregistered"
    unregistered.mkdir()

    with pytest.raises(ValueError, match="not registered"):
        inbox_send(
            db_conn,
            agent_cwd=str(unregistered),
            to="bob",
            subject="hello",
            body="body",
        )


def test_cross_agent_isolation_for_check_and_ack(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    owner = _register(db_conn, tmp_path, "owner", "owner")
    other = _register(db_conn, tmp_path, "other", "other")

    sent = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="owner",
        subject="private",
        body="body",
    )

    other_check = inbox_check(db_conn, agent_cwd=other["cwd"], limit=10)
    assert other_check["messages"] == []

    other_ack = inbox_ack(db_conn, agent_cwd=other["cwd"], message_ids=[sent["id"]])
    assert other_ack == {"updated": 0, "not_found": [sent["id"]]}

    owner_check = inbox_check(db_conn, agent_cwd=owner["cwd"], limit=10)
    assert [msg["id"] for msg in owner_check["messages"]] == [sent["id"]]


def test_unread_only_default_vs_include_read(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")
    first = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="first",
        body="body",
    )
    second = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="second",
        body="body",
    )
    inbox_ack(
        db_conn,
        agent_cwd=recipient["cwd"],
        message_ids=[first["id"]],
        status="read",
    )

    unread = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=10)
    assert [msg["id"] for msg in unread["messages"]] == [second["id"]]

    all_unacked = inbox_check(
        db_conn,
        agent_cwd=recipient["cwd"],
        limit=10,
        include_read=True,
    )
    assert {msg["id"] for msg in all_unacked["messages"]} == {
        first["id"],
        second["id"],
    }


def test_acked_messages_are_excluded_from_check(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")
    sent = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="done",
        body="body",
    )

    inbox_ack(db_conn, agent_cwd=recipient["cwd"], message_ids=[sent["id"]])

    out = inbox_check(
        db_conn,
        agent_cwd=recipient["cwd"],
        limit=10,
        include_read=True,
    )
    assert out["messages"] == []


def test_pagination_round_trip_without_overlap(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")
    sent_ids = [
        inbox_send(
            db_conn,
            agent_cwd=sender["cwd"],
            to="bob",
            subject=f"msg-{i}",
            body="body",
        )["id"]
        for i in range(5)
    ]

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        out = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=2, cursor=cursor)
        seen.extend(msg["id"] for msg in out["messages"])
        cursor = out["next_cursor"]
        if cursor is None:
            break

    assert set(seen) == set(sent_ids)
    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_body_truncation_default_and_full(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")
    big = "x" * 3_000
    inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="big",
        body=big,
    )

    truncated = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=1)
    msg = truncated["messages"][0]
    assert msg["body_truncated"] is True
    assert msg["body_length"] == 3_000
    assert len(msg["body"]) < 3_000

    full = inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=1, full=True)
    assert full["messages"][0]["body"] == big
    assert "body_truncated" not in full["messages"][0]


def test_ack_read_then_acked_transitions_and_not_found(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")
    _register(db_conn, tmp_path, "other", "other")
    first = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="first",
        body="body",
    )
    second = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="bob",
        subject="second",
        body="body",
    )
    foreign = inbox_send(
        db_conn,
        agent_cwd=sender["cwd"],
        to="other",
        subject="foreign",
        body="body",
    )

    read = inbox_ack(
        db_conn,
        agent_cwd=recipient["cwd"],
        message_ids=[first["id"], foreign["id"], "missing"],
        status="read",
    )
    assert read == {"updated": 1, "not_found": [foreign["id"], "missing"]}

    after_read = inbox_check(
        db_conn,
        agent_cwd=recipient["cwd"],
        limit=10,
        include_read=True,
    )
    first_msg = next(msg for msg in after_read["messages"] if msg["id"] == first["id"])
    assert first_msg["read_at"] is not None
    assert first_msg["acked_at"] is None

    acked = inbox_ack(
        db_conn,
        agent_cwd=recipient["cwd"],
        message_ids=[first["id"], second["id"]],
    )
    assert acked == {"updated": 2, "not_found": []}

    after_ack = inbox_check(
        db_conn,
        agent_cwd=recipient["cwd"],
        limit=10,
        include_read=True,
    )
    assert after_ack["messages"] == []

    rows = db_conn.execute(
        "select id, read_at, acked_at from inbox where id in (?, ?)",
        (first["id"], second["id"]),
    ).fetchall()
    by_id: dict[str, Any] = {row["id"]: row for row in rows}
    assert by_id[first["id"]]["read_at"] is not None
    assert by_id[first["id"]]["acked_at"] is not None
    assert by_id[second["id"]]["read_at"] is not None
    assert by_id[second["id"]]["acked_at"] is not None


def test_priority_limit_and_status_validation(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    recipient = _register(db_conn, tmp_path, "recipient", "bob")

    with pytest.raises(ValueError, match="priority"):
        inbox_send(
            db_conn,
            agent_cwd=sender["cwd"],
            to="bob",
            subject="bad",
            body="body",
            priority="bad",
        )
    with pytest.raises(ValueError, match="limit"):
        inbox_check(db_conn, agent_cwd=recipient["cwd"], limit=0)
    with pytest.raises(ValueError, match="status"):
        inbox_ack(db_conn, agent_cwd=recipient["cwd"], message_ids=["x"], status="bad")


def test_non_dict_metadata_raises(
    db_conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    sender = _register(db_conn, tmp_path, "sender", "alice")
    _register(db_conn, tmp_path, "recipient", "bob")

    with pytest.raises(ValueError, match="metadata"):
        inbox_send(
            db_conn,
            agent_cwd=sender["cwd"],
            to="bob",
            subject="hello",
            body="body",
            metadata=[1, 2],  # type: ignore[arg-type]
        )
