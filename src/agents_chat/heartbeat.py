"""
Heartbeat registry: track all running authors + their events.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .author.base import Author
from .monitor import Monitor
from .policy import NetworkPolicy, RateLimiter
from .storage.channels_db import ChannelDB
from .storage.posts_db import PostsDB


class HeartbeatRegistry:
    def __init__(
        self,
        policy: NetworkPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
        monitor: Monitor | None = None,
        posts: PostsDB | None = None,
        channels: ChannelDB | None = None,
    ):
        self.authors: dict[str, Author] = {}
        self.policy = policy or NetworkPolicy()
        self.rate_limiter = rate_limiter
        self.monitor = monitor
        self.posts = posts
        self.channels = channels

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
        if a := self.authors.get(author_id):
            a.trigger_immediate_tick()

    def trigger_burst_all(self):
        for a in self.authors.values():
            a.trigger_immediate_tick()

    def snapshots(self) -> list[dict[str, Any]]:
        return [a.snapshot() for a in self.authors.values()]

    # ------------------------------------------------------------------
    # Free Chat (新: 走 PostsDB, kind=freechat)
    # ------------------------------------------------------------------

    def start_free_chat(self, topic: str, started_by: str = "god", max_rounds: int = 10) -> dict:
        """开始一个 free chat session (新实现: 走 PostsDB)."""
        if not self.posts:
            return {"error": "posts db not configured"}
        post = self.posts.new(
            kind="freechat",
            title=topic,
            body=f"Free chat started by {started_by}",
            posted_by=started_by,
            max_rounds=max_rounds,
            expires_in_seconds=120,
        )
        import asyncio
        asyncio.create_task(self.posts.post(post))
        # burst all
        self.trigger_burst_all()
        return post.to_dict()

    def free_chat_status(self) -> dict:
        """查询 active free chats."""
        if not self.posts:
            return {"active": False}
        import asyncio
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # 在 async context, 但不能 await 跨 event loop
            # 用简单方法: 通过 monitor 缓存
            return {"active": "unknown", "method": "async-context"}
        return {"active": "check-posts-db"}
