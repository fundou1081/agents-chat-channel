"""
ChannelDB: 持久公共频道 (Slack style).

3 张表:
  channels:         频道元数据
  channel_members:  订阅关系
  channel_messages: 消息历史

跟 PostsDB 区别:
  - PostsDB: pull (作者 tick 时扫)
  - ChannelDB: push (新消息就 burst tick 订阅者)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import aiosqlite

from ..models import Channel, ChannelMessage


SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,           -- "#frontend"
    description TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    is_public INTEGER DEFAULT 1,
    pinned_topic TEXT
);

CREATE TABLE IF NOT EXISTS channel_members (
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    PRIMARY KEY (channel_id, author_id)
);

CREATE TABLE IF NOT EXISTS channel_messages (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    sender TEXT NOT NULL,
    body TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    reply_to TEXT,
    mentions TEXT                          -- JSON array
);

CREATE INDEX IF NOT EXISTS idx_ch_members ON channel_members(author_id);
CREATE INDEX IF NOT EXISTS idx_ch_msgs ON channel_messages(channel_id, posted_at);
"""


class ChannelDB:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    async def _ensure_schema(self):
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True

    # ------------------------------------------------------------------
    # Channel CRUD
    # ------------------------------------------------------------------

    async def create_channel(self, channel: Channel) -> Channel:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """
                    INSERT INTO channels
                    (id, name, description, created_by, created_at, is_public, pinned_topic)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel.id, channel.name, channel.description,
                        channel.created_by, channel.created_at,
                        1 if channel.is_public else 0,
                        channel.pinned_topic,
                    ),
                )
            except aiosqlite.IntegrityError as e:
                raise ValueError(f"Channel name '{channel.name}' already exists") from e
            await db.commit()
        return channel

    @staticmethod
    def new_channel(
        name: str,
        description: str = "",
        created_by: str = "god",
        is_public: bool = True,
        pinned_topic: str = "",
    ) -> Channel:
        return Channel(
            id=str(uuid.uuid4())[:12],
            name=name,
            description=description,
            created_by=created_by,
            created_at=datetime.now().isoformat(),
            is_public=is_public,
            pinned_topic=pinned_topic,
        )

    async def list_channels(self) -> list[Channel]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channels ORDER BY name")
            rows = await cursor.fetchall()
        return [self._row_to_channel(r) for r in rows]

    async def get(self, channel_id: str) -> Channel | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_channel(row)

    async def get_by_name(self, name: str) -> Channel | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM channels WHERE name = ?", (name,))
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_channel(row)

    async def list_for_author(self, author_id: str) -> list[Channel]:
        """列出我订阅的所有频道."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT c.* FROM channels c
                JOIN channel_members m ON c.id = m.channel_id
                WHERE m.author_id = ?
                ORDER BY c.name
                """,
                (author_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_channel(r) for r in rows]

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------

    async def join(self, channel_id: str, author_id: str) -> bool:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO channel_members (channel_id, author_id, joined_at) VALUES (?, ?, ?)",
                    (channel_id, author_id, datetime.now().isoformat()),
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False  # already joined

    async def leave(self, channel_id: str, author_id: str) -> bool:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM channel_members WHERE channel_id = ? AND author_id = ?",
                (channel_id, author_id),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def list_members(self, channel_id: str) -> list[str]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT author_id FROM channel_members WHERE channel_id = ? ORDER BY joined_at",
                (channel_id,),
            )
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def post_message(self, msg: ChannelMessage) -> ChannelMessage:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO channel_messages
                (id, channel_id, sender, body, posted_at, reply_to, mentions)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    msg.id, msg.channel_id, msg.sender, msg.body, msg.posted_at,
                    msg.reply_to, json.dumps(msg.mentions),
                ),
            )
            await db.commit()
        return msg

    @staticmethod
    def new_message(
        channel_id: str,
        sender: str,
        body: str,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
    ) -> ChannelMessage:
        # 自动从 body 解析 @mentions
        if mentions is None:
            mentions = []
            for word in body.split():
                if word.startswith("@"):
                    mentions.append(word[1:].rstrip(",.!?:;"))
        return ChannelMessage(
            id=str(uuid.uuid4())[:12],
            channel_id=channel_id,
            sender=sender,
            body=body,
            posted_at=datetime.now().isoformat(),
            reply_to=reply_to,
            mentions=mentions,
        )

    async def list_messages(
        self, channel_id: str, limit: int = 50, since: str | None = None
    ) -> list[ChannelMessage]:
        await self._ensure_schema()
        sql = "SELECT * FROM channel_messages WHERE channel_id = ?"
        params: list = [channel_id]
        if since:
            sql += " AND posted_at > ?"
            params.append(since)
        sql += " ORDER BY posted_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    async def get_recent_for_authors(
        self, author_ids: list[str], since: str | None = None, limit: int = 50
    ) -> list[ChannelMessage]:
        """列出这些 author 订阅的频道里, since 之后的新消息."""
        if not author_ids:
            return []
        await self._ensure_schema()
        # 先找订阅的 channel_ids
        placeholders = ",".join("?" * len(author_ids))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"SELECT DISTINCT channel_id FROM channel_members WHERE author_id IN ({placeholders})",
                author_ids,
            )
            rows1 = await cursor.fetchall()
            channel_ids = [r[0] for r in rows1]
            if not channel_ids:
                return []
            ph2 = ",".join("?" * len(channel_ids))
            sql = f"SELECT * FROM channel_messages WHERE channel_id IN ({ph2})"
            params2: list = list(channel_ids)
            if since:
                sql += " AND posted_at > ?"
                params2.append(since)
            sql += " ORDER BY posted_at DESC LIMIT ?"
            params2.append(limit)
            cursor = await db.execute(sql, params2)
            rows = await cursor.fetchall()
        return [self._row_to_msg(r) for r in rows]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_channel(row) -> Channel:
        # 兼容 is_public 字段 (0/1)
        is_pub = row["is_public"]
        is_public = bool(is_pub) if isinstance(is_pub, int) else (is_pub == 1)
        return Channel(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            created_by=row["created_by"],
            created_at=row["created_at"],
            is_public=is_public,
            pinned_topic=row["pinned_topic"] or "",
        )

    @staticmethod
    def _row_to_msg(row) -> ChannelMessage:
        return ChannelMessage(
            id=row["id"],
            channel_id=row["channel_id"],
            sender=row["sender"],
            body=row["body"] or "",
            posted_at=row["posted_at"],
            reply_to=row["reply_to"],
            mentions=json.loads(row["mentions"] or "[]"),
        )
