"""
Posts DB: 合并 Bulletin (永久) + FreeChat (临时) 为 1 张 posts 表.

跟老 Bulletin 的区别:
  - 新增 `kind`: "broadcast" | "task" | "discussion" | "freechat"
  - 新增 lifecycle 字段: max_rounds / current_round / session_id
  - kind="freechat" 用 current_round + max_rounds 实现"10 轮自动结束"
  - kind="task" 仍用 claimed_by (认领机制)
  - kind="broadcast" 仍是 open → close 手动

跟老 FreeChat 的区别:
  - 之前 FreeChatManager 是 in-memory, 现在持久化到 SQLite
  - 多个进程重启后 freechat 状态保留
  - 同一 post 关联到 session, 关闭 freechat 归档消息历史

relevance 逻辑跟老 Bulletin 一样 (按 role/mention 匹配).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from ..models import Post

if TYPE_CHECKING:
    from ..models import Persona


SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,           -- "broadcast" | "task" | "discussion" | "freechat"
    title TEXT,
    body TEXT,
    posted_by TEXT NOT NULL,
    posted_at TEXT NOT NULL,
    tags TEXT,                    -- JSON array
    required_role TEXT,
    claimed_by TEXT,
    status TEXT DEFAULT 'open',   -- "open" | "claimed" | "closed" | "expired"
    expires_at TEXT,              -- freechat 才有 (TTL)
    max_rounds INT,               -- freechat 才有 (10)
    current_round INT,            -- freechat 才有
    session_id TEXT,              -- freechat 关联 session
    last_activity_at TEXT         -- freechat idle 判定
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, posted_at);
CREATE INDEX IF NOT EXISTS idx_posts_kind ON posts(kind, status);
CREATE INDEX IF NOT EXISTS idx_posts_session ON posts(session_id);
"""


class PostsDB:
    """合并 Bulletin + FreeChat 的统一 Posts 存储.

    4 个 kind 共享 schema, 但 lifecycle 不同.
    """

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

    async def post(self, post: Post) -> Post:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO posts
                (id, kind, title, body, posted_by, posted_at, tags, required_role,
                 claimed_by, status, expires_at, max_rounds, current_round,
                 session_id, last_activity_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.id, post.kind, post.title, post.body, post.posted_by, post.posted_at,
                    json.dumps(post.tags), post.required_role, post.claimed_by,
                    post.status, post.expires_at, post.max_rounds, post.current_round,
                    post.session_id, post.last_activity_at,
                ),
            )
            await db.commit()
        return post

    @staticmethod
    def new(
        kind: str,
        title: str = "",
        body: str = "",
        posted_by: str = "god",
        tags: list[str] | None = None,
        required_role: str = "",
        expires_in_seconds: int = 0,
        max_rounds: int = 0,
        session_id: str = "",
    ) -> Post:
        """便捷构造器 (兼容老 BulletinDB.new + FreeChat 临时)."""
        return Post(
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
            max_rounds=max_rounds,
            current_round=0,
            session_id=session_id,
            last_activity_at=datetime.now().isoformat(),
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get(self, post_id: str) -> Post | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_post(row)

    async def list_open(
        self, kind: str | None = None, limit: int = 50
    ) -> list[Post]:
        await self._ensure_schema()
        sql = "SELECT * FROM posts WHERE status = 'open'"
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
        return [self._row_to_post(r) for r in rows]

    async def list_all(self, limit: int = 100) -> list[Post]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM posts ORDER BY posted_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        return [self._row_to_post(r) for r in rows]

    async def list_claimed_by(self, claimer: str) -> list[Post]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM posts WHERE claimed_by = ? ORDER BY posted_at DESC",
                (claimer,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_post(r) for r in rows]

    async def list_active_freechats(self) -> list[Post]:
        """返回所有 active freechat posts (kind=freechat, status=open)."""
        return await self.list_open(kind="freechat", limit=20)

    async def list_for_author(
        self, persona: "Persona", limit: int = 20
    ) -> list[Post]:
        """返回对某 author 相关的开放 post.

        relevance 逻辑 (跟老 Bulletin 一样):
        - broadcast: 所有人
        - task: role 匹配 / "any" / 空
        - discussion: mentions me
        - freechat: 所有人 (active 时)
        """
        await self.expire_old()
        items = await self.list_open(limit=200)
        relevant = [i for i in items if self._is_relevant(i, persona)]
        return relevant[:limit]

    # ------------------------------------------------------------------
    # Relevance
    # ------------------------------------------------------------------

    def _is_relevant(self, post: Post, persona) -> bool:
        if post.kind == "broadcast":
            return True
        if post.kind in ("task", "unassigned_task"):
            if post.required_role in ("", "any"):
                return True
            role = post.required_role.lower()
            if role in persona.title.lower() or role in persona.id.lower():
                return True
            return False
        if post.kind == "discussion":
            return self._mentions(post, persona)
        if post.kind == "freechat":
            return True  # active freechat 任何人都看
        return True

    def _mentions(self, post: Post, persona) -> bool:
        text = f"{post.title} {post.body} {post.required_role}".lower()
        for tag in post.tags:
            text += " " + tag.lower()
        for c in [persona.id, persona.display_name, persona.title]:
            if c and c.lower() in text:
                return True
        return False

    # ------------------------------------------------------------------
    # 状态变更
    # ------------------------------------------------------------------

    async def claim(self, post_id: str, claimer: str) -> tuple[bool, str]:
        """task 认领. 原子."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
            row = await cursor.fetchone()
            if not row:
                return False, "not found"
            if row["status"] != "open":
                return False, f"already {row['status']} by {row['claimed_by']}"
            cursor = await db.execute(
                """
                UPDATE posts SET status = 'claimed', claimed_by = ?
                WHERE id = ? AND status = 'open'
                """,
                (claimer, post_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return False, "race: someone else claimed it"
        return True, "ok"

    async def close(self, post_id: str) -> bool:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE posts SET status = 'closed' WHERE id = ? AND status != 'closed'",
                (post_id,),
            )
            await db.commit()
        return cursor.rowcount > 0

    async def record_freechat_round(
        self, post_id: str, new_round: int, last_activity_at: str
    ) -> bool:
        """freechat 记录一轮新消息, 判定是否要 close."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE posts SET current_round = ?, last_activity_at = ? WHERE id = ?",
                (new_round, last_activity_at, post_id),
            )
            await db.commit()
        post = await self.get(post_id)
        if post and post.max_rounds and post.current_round >= post.max_rounds:
            await self.close(post_id)
            return False  # ended
        return True  # still active

    async def expire_old(self) -> int:
        """expire 检查: 超过 expires_at 的 → expired; freechat idle 超 120s 也 expired."""
        await self._ensure_schema()
        now = datetime.now()
        now_iso = now.isoformat()
        # 1) expires_at 过期
        # 2) freechat last_activity_at 超过 120s
        idle_threshold = (now - timedelta(seconds=120)).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                UPDATE posts SET status = 'expired'
                WHERE status = 'open' AND (
                    (expires_at != '' AND expires_at < ?)
                    OR (kind = 'freechat' AND last_activity_at != '' AND last_activity_at < ?)
                )
                """,
                (now_iso, idle_threshold),
            )
            await db.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
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
            max_rounds=row["max_rounds"] or 0,
            current_round=row["current_round"] or 0,
            session_id=row["session_id"] or "",
            last_activity_at=row["last_activity_at"] or "",
        )
