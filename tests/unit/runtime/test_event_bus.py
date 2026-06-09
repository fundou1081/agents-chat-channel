"""
EventBus 单元测试.

覆盖:
- emit + wait 0 延迟唤醒 (< 50ms)
- wait timeout 兜底
- 多订阅者广播
- 跨 loop 安全 (pytest-asyncio function scope 模拟)
- 跟 Channel.append() 集成
- 跟 Mailbox.append() 集成
"""
from __future__ import annotations

import asyncio
import time

import pytest

from agents_chat.infra.events import (
    EventBus,
    channel_event,
    get_event_bus,
    mailbox_event,
    task_event,
)


# =============================================================================
# 基础功能
# =============================================================================


class TestEventBusBasics:
    def test_singleton(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2

    def test_stats(self):
        bus = EventBus()
        assert bus.stats()["tracked_events"] == 0


# =============================================================================
# emit + wait 0 延迟
# =============================================================================


class TestEventBusWait:
    @pytest.mark.asyncio
    async def test_emit_wakes_wait_immediately(self):
        bus = EventBus()
        # 在 loop 内 wait 之前先 emit
        bus.emit("test:event")
        # wait 应立即返回 True (< 50ms)
        start = time.monotonic()
        fired = await bus.wait("test:event", timeout=1.0)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert fired is True
        assert elapsed_ms < 50, f"wait took {elapsed_ms:.1f}ms, expected < 50ms"

    @pytest.mark.asyncio
    async def test_emit_after_wait_wakes_immediately(self):
        """wait() 在前, emit() 在后 — wait 应被唤醒, 0 延迟."""
        bus = EventBus()
        received = asyncio.Event()

        async def waiter():
            fired = await bus.wait("test:wake", timeout=2.0)
            assert fired is True
            received.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # 让 waiter 真的进入 wait
        bus.emit("test:wake")
        await asyncio.wait_for(received.wait(), timeout=1.0)
        await task

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        bus = EventBus()
        start = time.monotonic()
        fired = await bus.wait("test:never", timeout=0.1)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert fired is False
        assert 80 < elapsed_ms < 200, f"timeout {elapsed_ms:.1f}ms 偏离 100ms"

    @pytest.mark.asyncio
    async def test_clear_resets_event(self):
        bus = EventBus()
        bus.emit("test:clear")
        assert await bus.wait("test:clear", timeout=0.1) is True
        bus.clear("test:clear")
        # 下一轮 wait 应该 block (因为没新 emit)
        start = time.monotonic()
        fired = await bus.wait("test:clear", timeout=0.1)
        elapsed_ms = (time.monotonic() - start) * 1000
        assert fired is False
        assert elapsed_ms >= 90


# =============================================================================
# 多订阅者
# =============================================================================


class TestEventBusBroadcast:
    @pytest.mark.asyncio
    async def test_multiple_waiters_all_woken(self):
        bus = EventBus()
        woken = [False, False, False]

        async def make_waiter(idx: int):
            fired = await bus.wait("test:broadcast", timeout=1.0)
            woken[idx] = fired

        tasks = [asyncio.create_task(make_waiter(i)) for i in range(3)]
        await asyncio.sleep(0.05)  # 让所有 waiter 进入 wait
        bus.emit("test:broadcast")
        await asyncio.gather(*tasks)
        assert all(woken), f"not all woken: {woken}"


# =============================================================================
# 事件名构造器
# =============================================================================


class TestEventNameHelpers:
    def test_mailbox_event(self):
        assert mailbox_event("seller-fish") == "mailbox:seller-fish:new"

    def test_channel_event(self):
        assert channel_event("fish-market") == "channel:fish-market:new"

    def test_task_event(self):
        assert task_event("t_001") == "task:t_001:update"


# =============================================================================
# 跨 loop 安全 (singleton 在多个 test loop 间共享)
# =============================================================================


class TestEventBusCrossLoop:
    @pytest.mark.asyncio
    async def test_loop1_emit_loop1_wait(self):
        bus = get_event_bus()  # singleton
        bus.emit("test:loop1:only")
        fired = await bus.wait("test:loop1:only", timeout=0.5)
        assert fired is True
        bus.clear("test:loop1:only")

    @pytest.mark.asyncio
    async def test_loop2_emit_loop2_wait_after_loop1(self):
        """模拟 pytest-asyncio 多个 test (每个 function scope 独立 loop)."""
        bus = get_event_bus()  # 同一个 singleton
        bus.emit("test:loop2:only")
        fired = await bus.wait("test:loop2:only", timeout=0.5)
        assert fired is True
        bus.clear("test:loop2:only")


# =============================================================================
# drain (重置)
# =============================================================================


class TestEventBusDrain:
    @pytest.mark.asyncio
    async def test_drain_clears_all(self):
        bus = EventBus()
        bus.emit("a")
        bus.emit("b")
        bus.emit("c")
        bus.drain()
        # drain 后 wait 应该都 timeout
        a = await bus.wait("a", timeout=0.05)
        b = await bus.wait("b", timeout=0.05)
        c = await bus.wait("c", timeout=0.05)
        assert a is False and b is False and c is False


# =============================================================================
# 跟 Channel/Mailbox 集成
# =============================================================================


class TestChannelIntegration:
    def test_channel_append_emits_event(self, tmp_path):
        from agents_chat.infra.files import Channel
        from agents_chat.infra.events import get_event_bus, channel_event

        ch_path = tmp_path / "ch.jsonl"
        ch = Channel(ch_path, "demo-ch")

        bus = get_event_bus()
        bus.clear(channel_event("demo-ch"))

        ch.append(from_="god", content="hi", mentions=[])

        # 同步 emit 后, 在事件循环里 wait 应该立即返回
        async def check():
            return await bus.wait(channel_event("demo-ch"), timeout=0.5)

        fired = asyncio.run(check())
        assert fired is True


class TestMailboxIntegration:
    def test_mailbox_append_emits_event(self, tmp_path):
        from agents_chat.infra.files import Mailbox
        from agents_chat.infra.events import get_event_bus, mailbox_event

        mb_path = tmp_path / "mb.json"
        mb = Mailbox(mb_path, "test-agent")

        bus = get_event_bus()
        bus.clear(mailbox_event("test-agent"))

        mb.append(type="mention", content="hi", channel="demo")

        async def check():
            return await bus.wait(mailbox_event("test-agent"), timeout=0.5)

        fired = asyncio.run(check())
        assert fired is True
