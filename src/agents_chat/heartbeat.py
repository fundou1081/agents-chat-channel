"""
Heartbeat registry: track all running authors + their events.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .author.base import Author
from .monitor import Monitor
from .policy import FreeChatManager, NetworkPolicy, RateLimiter


class HeartbeatRegistry:
    """管理所有运行中的 author, 提供 broadcast 触发."""

    def __init__(self, policy: NetworkPolicy | None = None, rate_limiter: RateLimiter | None = None, monitor: Monitor | None = None):
        self.authors: dict[str, Author] = {}
        self.policy = policy or NetworkPolicy()
        self.rate_limiter = rate_limiter
        self.monitor = monitor
        self.free_chat = FreeChatManager(self.policy)

    def register(self, author: Author):
        self.authors[author.persona.id] = author

    def unregister(self, author_id: str):
        self.authors.pop(author_id, None)

    def get(self, author_id: str) -> Author | None:
        return self.authors.get(author_id)

    async def start_all(self):
        for a in self.authors.values():
            await a.start()

    async def stop_all(self):
        for a in self.authors.values():
            await a.stop()

    def trigger_burst(self, author_id: str):
        """新邮件触发的 burst tick."""
        if a := self.authors.get(author_id):
            a.trigger_immediate_tick()

    def trigger_burst_all(self):
        """广播给所有 author (自由聊天用)."""
        for a in self.authors.values():
            a.trigger_immediate_tick()

    def snapshots(self) -> list[dict[str, Any]]:
        return [a.snapshot() for a in self.authors.values()]

    def start_free_chat(self, topic: str, started_by: str = "god") -> dict:
        """开始一个 free chat session. 广播给所有 author."""
        authors = list(self.authors.keys())
        sess = self.free_chat.trigger(topic, started_by, authors)
        self.trigger_burst_all()
        return sess.to_dict()

    def free_chat_status(self) -> dict:
        """查询当前 free chat 状态."""
        # 检查 idle
        self.free_chat.check_idle()
        return self.free_chat.to_dict()
