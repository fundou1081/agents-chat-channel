"""Test network policy + rate limiter + free chat."""
import time
from datetime import datetime, timedelta

import pytest

from agents_chat.v1.policy import NetworkPolicy, RateLimiter, FreeChatManager


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






