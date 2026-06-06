"""
Heartbeat registry: track all running authors + their events.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .author.base import Author


class HeartbeatRegistry:
    """管理所有运行中的 author, 提供 broadcast 触发."""

    def __init__(self):
        self.authors: dict[str, Author] = {}

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

    def snapshots(self) -> list[dict[str, Any]]:
        return [a.snapshot() for a in self.authors.values()]
