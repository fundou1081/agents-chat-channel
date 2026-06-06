"""
SQLite-backed mailbox.

Each author has their own mailbox. Mails are persisted across restarts.
Receivers pull unread mails on their own heartbeat.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from ..models import Mail, MailPriority


SCHEMA = """
CREATE TABLE IF NOT EXISTS mails (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,        -- JSON array
    thread_id TEXT NOT NULL,
    in_reply_to TEXT,
    subject TEXT,
    body TEXT,
    attachments TEXT,                -- JSON
    priority INTEGER,
    requires_ack INTEGER,
    created_at TEXT NOT NULL,
    read_at TEXT,
    acked_at TEXT,
    metadata TEXT,                   -- JSON
    delivered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mails_recipient_unread
    ON mails(recipients, read_at, created_at);

CREATE INDEX IF NOT EXISTS idx_mails_thread
    ON mails(thread_id, created_at);

CREATE INDEX IF NOT EXISTS idx_mails_sender
    ON mails(sender, created_at);
"""


class MailboxDB:
    """Per-author mailbox. Can be shared (one DB, all authors) or split (per-author)."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False
        # 提前初始化 schema (避免第一次查询时表不存在)
        # 使用 anyio/thread 简单同步启动
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 在运行的 loop 里,延迟到首次查询
                pass
            else:
                loop.run_until_complete(self._ensure_schema())
        except RuntimeError:
            # 没有 event loop, 跳过 (后续首次查询时会创建)
            pass

    async def _ensure_schema(self):
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True

    async def deliver(self, mail: Mail) -> None:
        """存入 mailbox,recipients 各收到一份。"""
        await self._ensure_schema()
        now_iso = datetime.now().isoformat()
        recipients_json = json.dumps(list(mail.recipients))
        attachments_json = json.dumps(list(mail.attachments))
        metadata_json = json.dumps(mail.metadata)

        async with aiosqlite.connect(self.db_path) as db:
            # 存储一封 mail,recipients 数组
            await db.execute(
                """
                INSERT OR IGNORE INTO mails
                (id, sender, recipients, thread_id, in_reply_to, subject, body,
                 attachments, priority, requires_ack, created_at, metadata, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mail.id,
                    mail.sender,
                    recipients_json,
                    mail.thread_id,
                    mail.in_reply_to,
                    mail.subject,
                    mail.body,
                    attachments_json,
                    int(mail.priority),
                    int(mail.requires_ack),
                    mail.created_at.isoformat(),
                    metadata_json,
                    now_iso,
                ),
            )
            await db.commit()

    async def fetch_unread(
        self, owner: str, since: datetime | None = None, limit: int = 100
    ) -> list[Mail]:
        """拉取 owner 邮箱中未读的邮件,按时间排序。"""
        await self._ensure_schema()
        since_iso = since.isoformat() if since else "1970-01-01"
        # 用 JSON LIKE 匹配 recipients 数组中包含 owner
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM mails
                WHERE (recipients LIKE ? OR recipients LIKE ? OR recipients LIKE ?)
                  AND read_at IS NULL
                  AND created_at >= ?
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (f'%"{owner}"%', f'["{owner}"%', f'%"{owner}"]%', since_iso, limit),
            )
            rows = await cursor.fetchall()

        return [self._row_to_mail(r) for r in rows]

    async def fetch_thread(self, thread_id: str) -> list[Mail]:
        """拉取某个 thread 的所有邮件 (按时间序)。"""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM mails WHERE thread_id = ? ORDER BY created_at ASC",
                (thread_id,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_mail(r) for r in rows]

    async def fetch_inbox(
        self, owner: str, limit: int = 200
    ) -> list[Mail]:
        """拉取 owner 收件箱所有邮件 (包括已读)。"""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM mails
                WHERE (recipients LIKE ? OR recipients LIKE ? OR recipients LIKE ?)
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f'%"{owner}"%', f'["{owner}"%', f'%"{owner}"]%', limit),
            )
            rows = await cursor.fetchall()
        return [self._row_to_mail(r) for r in rows]

    async def mark_read(self, mail_ids: list[str]) -> None:
        if not mail_ids:
            return
        await self._ensure_schema()
        now_iso = datetime.now().isoformat()
        placeholders = ",".join("?" * len(mail_ids))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE mails SET read_at = ? WHERE id IN ({placeholders})",
                [now_iso, *mail_ids],
            )
            await db.commit()

    async def mark_acked(self, mail_id: str) -> None:
        await self._ensure_schema()
        now_iso = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE mails SET acked_at = ? WHERE id = ?",
                (now_iso, mail_id),
            )
            await db.commit()

    async def count_unread(self, owner: str) -> int:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM mails
                WHERE (recipients LIKE ? OR recipients LIKE ? OR recipients LIKE ?)
                  AND read_at IS NULL
                """,
                (f'%"{owner}"%', f'["{owner}"%', f'%"{owner}"]%',),
            )
            row = await cursor.fetchone()
        return row[0] if row else 0

    @staticmethod
    def _row_to_mail(row) -> Mail:
        return Mail(
            id=row["id"],
            sender=row["sender"],
            recipients=tuple(json.loads(row["recipients"])),
            thread_id=row["thread_id"],
            in_reply_to=row["in_reply_to"],
            subject=row["subject"] or "",
            body=row["body"] or "",
            attachments=tuple(json.loads(row["attachments"] or "[]")),
            priority=row["priority"] or MailPriority.NORMAL,
            requires_ack=bool(row["requires_ack"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            metadata=json.loads(row["metadata"] or "{}"),
        )
