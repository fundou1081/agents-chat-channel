"""Test monitor: events, filtering, conversations."""
import json

import pytest

from agents_chat.models import Mail
from agents_chat.monitor import EXTERNAL_SENDERS, Event, Monitor


@pytest.fixture
def monitor(tmp_data_dir):
    m = Monitor(tmp_data_dir / "monitor.jsonl")
    return m


def test_record_event(monitor):
    ev = monitor.record("mail_sent", actor="zhang-frontend", summary="test")
    assert ev.id
    assert ev.timestamp
    assert ev.kind == "mail_sent"
    assert ev.actor == "zhang-frontend"


def test_mail_sent(monitor):
    m = Mail.new(sender="zhang-frontend", recipients=["li-backend"], subject="hello", body="hi")
    ev = monitor.mail_sent(m, by_author="zhang-frontend")
    assert ev.kind == "mail_sent"
    assert ev.mail_id == m.id
    assert ev.mail_to == ["li-backend"]
    assert "li-backend" in ev.summary
    assert "hello" in ev.summary


def test_read_recent(monitor):
    monitor.record("mail_sent", actor="zhang")
    monitor.record("mail_received", actor="li")
    monitor.record("session_completed", actor="zhang")
    events = monitor.read_recent(limit=10, only_agent=False)
    assert len(events) == 3
    # 按时间倒序
    assert events[0]["kind"] == "session_completed"


def test_filter_external_sender(monitor):
    """外部 sender (god) 排除."""
    monitor.record("mail_sent", actor="god", mail_from="god", mail_to=["pm"], mail_subject="hi")
    monitor.record("mail_sent", actor="zhang", mail_from="zhang-frontend", mail_to=["li-backend"], mail_subject="work")
    events = monitor.read_recent(only_agent=True)
    assert len(events) == 1
    assert events[0]["actor"] == "zhang"


def test_filter_external_recipient(monitor):
    """全部 recipient 是外部 → 排除."""
    monitor.record("mail_sent", actor="zhang", mail_from="zhang-frontend", mail_to=["god"], mail_subject="report")
    events = monitor.read_recent(only_agent=True)
    assert len(events) == 0  # 全 god → 排除


def test_filter_mixed_recipients(monitor):
    """recipient 包含 agent → 保留."""
    monitor.record("mail_sent", actor="zhang", mail_from="zhang", mail_to=["li-backend", "god"], mail_subject="mixed")
    events = monitor.read_recent(only_agent=True)
    assert len(events) == 1


def test_conversations_only_sent(monitor):
    """conversations 只返回 mail_sent."""
    monitor.mail_sent(Mail.new(sender="a", recipients=["b"], subject="x", body=""), "a")
    monitor.mail_received(Mail.new(sender="b", recipients=["a"], subject="y", body=""), "a")
    monitor.session_completed("a", "T-1")
    convs = monitor.read_conversations()
    assert len(convs) == 1
    assert convs[0]["kind"] == "mail_sent"


def test_stats(monitor):
    monitor.mail_sent(Mail.new(sender="zhang", recipients=["li"], subject="", body=""), "zhang")
    monitor.mail_sent(Mail.new(sender="li", recipients=["zhang"], subject="", body=""), "li")
    monitor.session_completed("zhang", "T-1")
    monitor.tool_used("zhang", "write", "hello.py")

    stats = monitor.stats()
    assert stats["agent_events"] == 4
    assert stats["by_kind"]["mail_sent"] == 2
    assert stats["by_kind"]["session_completed"] == 1
    assert stats["by_kind"]["tool_used"] == 1
    assert stats["by_actor"]["zhang"] == 3
    assert stats["by_actor"]["li"] == 1


def test_empty_monitor(tmp_data_dir):
    """不存在的文件返回空."""
    m = Monitor(tmp_data_dir / "nonexistent.jsonl")
    assert m.read_recent() == []
    assert m.read_conversations() == []
    assert m.stats()["agent_events"] == 0


def test_external_senders_constant():
    """确保外部 sender 列表存在且合理."""
    assert "god" in EXTERNAL_SENDERS
    assert "user" in EXTERNAL_SENDERS
