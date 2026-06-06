"""Test network policy + rate limiter + free chat."""
import time
from datetime import datetime, timedelta

import pytest

from agents_chat.policy import (
    FreeChatManager,
    FreeChatSession,
    NetworkPolicy,
    RateLimiter,
)


def test_network_policy_defaults():
    p = NetworkPolicy()
    assert p.max_mails_per_hour == 30
    assert p.max_mails_per_day == 200
    assert p.max_actions_per_tick == 3
    assert p.max_thread_rounds == 8
    assert p.min_tick_interval_seconds == 3
    assert p.free_chat_max_rounds == 10


def test_network_policy_to_dict():
    p = NetworkPolicy(max_mails_per_hour=10)
    d = p.to_dict()
    assert d["max_mails_per_hour"] == 10


@pytest.mark.asyncio
async def test_rate_limiter_increment(tmp_data_dir):
    rl = RateLimiter(tmp_data_dir / "rate.db")
    n = rl.increment("zhang-frontend")
    assert n == 1
    n = rl.increment("zhang-frontend", n=3)
    assert n == 4
    assert rl.get_count("zhang-frontend", "hour") == 4
    assert rl.get_count("zhang-frontend", "day") == 4


@pytest.mark.asyncio
async def test_rate_limiter_check(tmp_data_dir):
    rl = RateLimiter(tmp_data_dir / "rate.db")
    # 初始 ok
    ok, reason = rl.check("zhang-frontend", max_per_hour=5, max_per_day=100)
    assert ok
    # 用 5 次
    for _ in range(5):
        rl.increment("zhang-frontend")
    # 第 6 次应该失败
    ok, reason = rl.check("zhang-frontend", max_per_hour=5, max_per_day=100)
    assert not ok
    assert "hourly" in reason


@pytest.mark.asyncio
async def test_rate_limiter_different_authors(tmp_data_dir):
    rl = RateLimiter(tmp_data_dir / "rate.db")
    rl.increment("zhang-frontend", 5)
    rl.increment("li-backend", 3)
    assert rl.get_count("zhang-frontend", "hour") == 5
    assert rl.get_count("li-backend", "hour") == 3
    # 不互相影响
    ok, _ = rl.check("li-backend", max_per_hour=3, max_per_day=100)
    assert not ok  # li-backend 已用 3/3
    ok, _ = rl.check("zhang-frontend", max_per_hour=5, max_per_day=100)
    assert not ok  # zhang 5/5 也已到限 (>=)


# ---- FreeChatManager ----

def test_freechat_trigger():
    fc = FreeChatManager()
    sess = fc.trigger(topic="周会", started_by="god", authors=["pm", "zhang"])
    assert sess.topic == "周会"
    assert sess.started_by == "god"
    assert sess.participants == ["pm", "zhang"]
    assert sess.current_round == 0
    assert sess.status == "active"
    assert fc.get_active() is sess


def test_freechat_record_message():
    fc = FreeChatManager()
    fc.trigger("周会", "god", ["pm", "zhang"])
    still_active = fc.record_message("pm", "大家好")
    assert still_active
    sess = fc.get_active()
    assert sess.current_round == 1
    assert len(sess.messages) == 1
    assert sess.messages[0]["author"] == "pm"


def test_freechat_max_rounds_ends_session():
    fc = FreeChatManager(NetworkPolicy(free_chat_max_rounds=3))
    fc.trigger("t", "god", ["a", "b"])
    fc.record_message("a", "hi")
    fc.record_message("b", "hi")
    # 第 3 轮: 触发 end
    still_active = fc.record_message("a", "third")
    assert not still_active
    assert fc.get_active().status == "ended"


def test_freechat_check_idle():
    fc = FreeChatManager(NetworkPolicy(free_chat_idle_seconds=0))  # 立即 idle
    fc.trigger("t", "god", ["a"])
    time.sleep(0.1)
    ended = fc.check_idle()
    assert ended
    assert fc.get_active().status == "ended"


def test_freechat_to_dict():
    fc = FreeChatManager()
    fc.trigger("周会", "god", ["pm", "zhang"])
    fc.record_message("pm", "go")
    d = fc.to_dict()
    assert d["active"] is True
    assert d["session"]["topic"] == "周会"
    assert d["session"]["current_round"] == 1


def test_freechat_no_active():
    fc = FreeChatManager()
    d = fc.to_dict()
    assert d["active"] is False
