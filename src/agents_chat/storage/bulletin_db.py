"""
Bulletin Board: SQLite-backed 中央信息存储.

跟 mailbox (个人 inbox) 不同:
- BulletinDB 是**中央**存储, 所有 author 共享
- 作者 tick 时**主动扫** (不是被动收)
- 支持 "无主任务" (first-claim-first-served)

API:
  post(ann)               发布公告
  list_open(kind, role)   列出开放
  list_for_author(p)      列出对某 author 相关的
  claim(id, claimer)      认领 (原子操作, 防 race)
  close(id)               关闭
  expire_old()            自动 expire

author relevance 逻辑:
  broadcast: 所有人
  unassigned_task: role 匹配或 "any"
  discussion: mentions me (id/display_name/title)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from ..models import Announcement

if TYPE_CHECKING:
    from ..models import Persona


SCHEMA = """
CREATE TABLE IF NOT EXISTS bulletins (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- "broadcast" | "unassigned_task" | "discussion"
    title TEXT NOT NULL,
    body TEXT,
    posted_by TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    tags TEXT,                    -- JSON array
    required_role TEXT,           -- "" | "frontend" | "backend" | "any" | ...
    claimed_by TEXT,
    status TEXT DEFAULT 'open',   -- "open" | "claimed" | "closed" | "expired"
    expires_at TEXT,
    thread_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_bulletins_status ON bulletins(status, posted_at);
CREATE INDEX IF NOT EXISTS idx_bulletins_kind ON bulletins(kind, status);
CREATE INDEX IF NOT EXISTS idx_bulletins_claimed_by ON bulletins(claimed_by);
"""


class BulletinDB:
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
    # 写入
    # ------------------------------------------------------------------

    async def post(self, ann: Announcement) -> Announcement:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO bulletins
                (id, kind, title, body, posted_by, posted_at, tags, required_role,
                 claimed_by, status, expires_at, thread_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ann.id, ann.kind, ann.title, ann.body, ann.posted_by, ann.posted_at,
                    json.dumps(ann.tags), ann.required_role, ann.claimed_by,
                    ann.status, ann.expires_at, ann.thread_id,
                ),
            )
            await db.commit()
        return ann

    @staticmethod
    def new(
        kind: str,
        title: str,
        body: str,
        posted_by: str = "god",
        tags: list[str] | None = None,
        required_role: str = "",
        expires_in_seconds: int = 0,
        thread_id: str = "",
    ) -> Announcement:
        """便捷构造器."""
        return Announcement(
            id=str(uuid.uuid4())[:12],
            kind=kind,
            title=title,
            body=body,
            posted_by=posted_by,
            posted_at=datetime.now().isoformat(),
            tags=tags or [],
            required_role=required_role,
            expires_at=(
                (datetime.now() + timedelta(seconds=expires_in_seconds)).isoformat()
                if expires_in_seconds > 0 else ""
            ),
            thread_id=thread_id,
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get(self, ann_id: str) -> Announcement | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM bulletins WHERE id = ?", (ann_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ann(row)

    async def list_open(
        self, kind: str | None = None, limit: int = 50
    ) -> list[Announcement]:
        await self._ensure_schema()
        sql = "SELECT * FROM bulletins WHERE status = 'open'"
        params: list = []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        sql += " ORDER BY posted_at DESC LIMIT ?"
        params.append(limit)
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql, params)
            rows = await cursor.fetchall()
        return [self._row_to_ann(r) for r in rows]

    async def list_all(self, limit: int = 100) -> list[Announcement]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulletins ORDER BY posted_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [self._row_to_ann(r) for r in rows]

    async def list_claimed_by(self, claimer: str) -> list[Announcement]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulletins WHERE claimed_by = ? ORDER BY posted_at DESC",
                (claimer,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_ann(r) for r in rows]

    async def list_for_author(
        self, persona: "Persona", limit: int = 20
    ) -> list[Announcement]:
        """列出对某 author 相关的开放公告.

        策略:
        - broadcast: 所有人相关
        - unassigned_task: role 匹配或 "any" 或空
        - discussion: mentions me (id / display_name / title)
        """
        await self.expire_old()
        items = await self.list_open(limit=200)
        relevant = [i for i in items if self._is_relevant(i, persona)]
        return relevant[:limit]

    # ------------------------------------------------------------------
    # Relevance logic
    # ------------------------------------------------------------------

    def _is_relevant(self, ann: Announcement, persona) -> bool:
        if ann.kind == "broadcast":
            return True
        if ann.kind == "unassigned_task":
            if ann.required_role in ("", "any"):
                return True
            role = ann.required_role.lower()
            if role in persona.title.lower():
                return True
            if role in persona.id.lower():
                return True
            return False
        if ann.kind == "discussion":
            return self._mentions(ann, persona)
        # default: 任何都看
        return True

    def _mentions(self, ann: Announcement, persona) -> bool:
        text = f"{ann.title} {ann.body} {ann.required_role}".lower()
        for tag in ann.tags:
            text += " " + tag.lower()
        candidates = [persona.id, persona.display_name, persona.title]
        for c in candidates:
            if c and c.lower() in text:
                return True
        return False

    # ------------------------------------------------------------------
    # 状态变更
    # ------------------------------------------------------------------

    async def claim(self, ann_id: str, claimer: str) -> tuple[bool, str]:
        """尝试认领. 原子操作: status='open' 才能 claim."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM bulletins WHERE id = ?", (ann_id,)
            )
            row = await cursor.fetchone()
            if not row:
                return False, "not found"
            if row["status"] != "open":
                claimer_existing = row["claimed_by"] or "?"
                return False, f"already {row['status']} by {claimer_existing}"
            cursor = await db.execute(
                """
                UPDATE bulletins
                SET status = 'claimed', claimed_by = ?
                WHERE id = ? AND status = 'open'
                """,
                (claimer, ann_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return False, "race: someone else claimed it"
        return True, "ok"

    async def close(self, ann_id: str) -> bool:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE bulletins SET status = 'closed' WHERE id = ? AND status != 'closed'",
                (ann_id,),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def expire_old(self) -> int:
        """过期 status='open' 但 expires_at < now 的."""
        await self._ensure_schema()
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE bulletins SET status = 'expired'
                WHERE status = 'open' AND expires_at != '' AND expires_at < ?
                """,
                (now,),
            )
            await db.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_ann(row) -> Announcement:
        return Announcement(
            id=row["id"],
            kind=row["kind"],
            title=row["title"] or "",
            body=row["body"] or "",
            posted_by=row["posted_by"],
            posted_at=row["posted_at"],
            tags=json.loads(row["tags"] or "[]"),
            required_role=row["required_role"] or "",
            claimed_by=row["claimed_by"] or "",
            status=row["status"] or "open",
            expires_at=row["expires_at"] or "",
            thread_id=row["thread_id"] or "",
        )
