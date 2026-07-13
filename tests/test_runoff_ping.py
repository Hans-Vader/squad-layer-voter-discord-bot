"""Unit tests for the runoff ping-message builder (utils.build_ping_messages)."""

from utils import build_ping_messages


def test_empty_returns_no_messages():
    assert build_ping_messages([], [], "Runoff!") == []


def test_roles_and_users_formatted_and_ordered():
    msgs = build_ping_messages([10, 20], [1, 2], "Runoff!")
    assert msgs == ["Runoff!\n<@&10> <@&20> <@1> <@2>"]


def test_header_only_on_first_chunk():
    # limit=14 forces two mentions per message (see arithmetic in build_ping_messages)
    msgs = build_ping_messages([], [1, 2, 3, 4], "Head", limit=14)
    assert msgs == ["Head\n<@1> <@2>", "<@3> <@4>"]  # no header on later chunks


def test_no_message_exceeds_limit_and_all_ids_present():
    # 90 realistic 19-digit snowflakes — the case that broke a count-based chunker.
    users = [1234567890123456789 + i for i in range(90)]
    msgs = build_ping_messages([], users, "🔁 Runoff!")
    assert all(len(m) <= 2000 for m in msgs)
    joined = " ".join(msgs)
    assert all(f"<@{u}>" in joined for u in users)
