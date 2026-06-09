"""
进程内事件总线 (in-process EventBus).

替代 v1 时代 "Scanner 后台进程 + Mailbox 文件" 模型的进程内部分:
- Channel.append() / Mailbox.append() 写完后立即 emit 事件
- CommunicationComponent.listen() 用 wait() 等待, 0 延迟唤醒
- watchdog (FileBusWatcher) 监听文件系统事件, 跨进程触发

事件命名约定:
  - "mailbox:<agent_id>:new"     新邮件投递到某 agent mailbox
  - "channel:<name>:new"         某频道有新消息
  - "task:<task_id>:update"      state_board 某 task 状态变化
  - "stop"                       全局停止信号 (测试 + 优雅退出)

延迟:
  - 进程内 emit → wait: < 1ms
  - watchdog 跨进程: < 50ms (macOS FSEvents / Linux inotify)

设计:
  - 单例模式 (process-level), 通过 get_event_bus() 访问
  - asyncio 兼容 (用 asyncio.Event)
  - 同步 emit (在 Channel.append() 同步路径里调)
  - 异步 wait (在 CommunicationComponent.listen() 协程里调)
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any


# 全局单例
_event_bus: "EventBus | None" = None
_event_bus_lock = threading.Lock()


class EventBus:
    """进程内事件总线 — emit (同步) → wait (异步) 0 延迟唤醒.

    Usage:
        bus = get_event_bus()  # 单例
        bus.emit("mailbox:seller-fish:new")
        ...
        ok = await bus.wait("mailbox:seller-fish:new", timeout=1.0)

    跨 loop 安全:
        asyncio.Event 绑创建它的 event loop, 跨 loop 使用会报错.
        本 bus 按 loop_id 分组管理 Event — 多个 loop (如 pytest-asyncio
        function scope) 都能安全使用同一个 singleton.
    """

    def __init__(self) -> None:
        # loop_id -> {event_name -> Event}
        self._events_by_loop: dict[int, dict[str, asyncio.Event]] = {}
        # loop-agnostic: emit 后 wait 立即返 True (处理 emit-then-wait 顺序)
        self._pending: dict[str, bool] = {}
        self._lock = threading.Lock()

    def _get_or_create_event(self, name: str) -> asyncio.Event:
        """获取/创建 event (绑当前 running loop)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没在 loop 里, 用一个伪 loop_id (signal-only emit, wait 会报错)
            # 实际业务不会遇到 (emit 同步, wait 异步)
            loop = None
        loop_id = id(loop) if loop is not None else 0

        bucket = self._events_by_loop.get(loop_id)
        if bucket is None:
            with self._lock:
                bucket = self._events_by_loop.get(loop_id)
                if bucket is None:
                    bucket = {}
                    self._events_by_loop[loop_id] = bucket
        ev = bucket.get(name)
        if ev is not None:
            return ev
        with self._lock:
            ev = bucket.get(name)
            if ev is not None:
                return ev
            # Event 必须在 loop 里创建 — 这里肯定在 loop 里 (上面 get_running_loop 过了)
            ev = asyncio.Event()
            bucket[name] = ev
            return ev

    def emit(self, event: str, payload: Any = None) -> None:
        """同步触发事件 (在 Channel.append() 同步路径里调).

        广播到所有 loop (多 loop 场景, 如 pytest-asyncio 不同 test).
        处理 emit-then-wait 顺序: 即使还没 waiter, 下一轮 wait 也立即返.
        """
        with self._lock:
            self._pending[event] = True
            buckets = list(self._events_by_loop.values())
        for bucket in buckets:
            ev = bucket.get(event)
            if ev is not None:
                try:
                    ev.set()
                except Exception:
                    pass

    def clear(self, event: str) -> None:
        """清空事件标志 (wait() 完调, 准备下一轮)."""
        self._pending.pop(event, None)
        try:
            loop = asyncio.get_running_loop()
            bucket = self._events_by_loop.get(id(loop), {})
            ev = bucket.get(event)
            if ev is not None:
                ev.clear()
        except RuntimeError:
            pass

    async def wait(self, event: str, timeout: float | None = None) -> bool:
        """异步等待事件.

        Returns:
            True  - 事件被触发 (0 延迟唤醒, 或 emit-then-wait 顺序)
            False - timeout 到达
        """
        # 处理 emit-then-wait: pending 立即返 (避免起 Event 后发现早已 set)
        if self._pending.pop(event, False):
            return True

        ev = self._get_or_create_event(event)
        if timeout is None:
            await ev.wait()
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def drain(self) -> None:
        """清空所有事件 (测试用, 重置状态)."""
        with self._lock:
            self._pending.clear()
            for bucket in self._events_by_loop.values():
                for ev in bucket.values():
                    try:
                        ev.clear()
                    except Exception:
                        pass

    def stats(self) -> dict[str, int]:
        """诊断: 各事件触发次数 (cumulative since bus creation)."""
        total = sum(len(b) for b in self._events_by_loop.values())
        return {
            "tracked_events": total,
            "active_loops": len(self._events_by_loop),
        }


def get_event_bus() -> EventBus:
    """获取 process-level 单例 EventBus (线程安全 lazy init)."""
    global _event_bus
    if _event_bus is None:
        with _event_bus_lock:
            if _event_bus is None:
                _event_bus = EventBus()
    return _event_bus


# 事件名构造器 (避免字符串拼写错误)
def mailbox_event(agent_id: str) -> str:
    return f"mailbox:{agent_id}:new"


def channel_event(channel_name: str) -> str:
    return f"channel:{channel_name}:new"


def task_event(task_id: str) -> str:
    return f"task:{task_id}:update"
