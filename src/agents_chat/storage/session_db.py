"""
Session persistence: each author owns their session contexts.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from ..models import SessionContext


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    owner TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    topic TEXT,
    status TEXT,
    participants TEXT,           -- JSON array
    history_ids TEXT,             -- JSON array
    blocked_reason TEXT,
    last_activity TEXT,
    created_at TEXT,
    summary TEXT,
    PRIMARY KEY (owner, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_owner_status
    ON sessions(owner, status);
"""


class SessionDB:
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

    async def upsert(self, owner: str, session: SessionContext) -> None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT OR REPLACE INTO sessions
                (owner, thread_id, topic, status, participants, history_ids,
                 blocked_reason, last_activity, created_at, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    owner,
                    session.thread_id,
                    session.topic,
                    session.status,
                    json.dumps(sorted(session.participants)),
                    json.dumps(session.history_ids),
                    session.blocked_reason,
                    session.last_activity.isoformat(),
                    session.created_at.isoformat(),
                    session.summary,
                ),
            )
            await db.commit()

    async def get(self, owner: str, thread_id: str) -> SessionContext | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE owner = ? AND thread_id = ?",
                (owner, thread_id),
            )
            row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_session(row)

    async def list_active(self, owner: str) -> list[SessionContext]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM sessions
                WHERE owner = ? AND status IN ('active', 'blocked')
                ORDER BY last_activity DESC
                """,
                (owner,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_session(r) for r in rows]

    async def list_all(self, owner: str) -> list[SessionContext]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM sessions WHERE owner = ? ORDER BY last_activity DESC",
                (owner,),
            )
            rows = await cursor.fetchall()
        return [self._row_to_session(r) for r in rows]

    async def delete(self, owner: str, thread_id: str) -> None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM sessions WHERE owner = ? AND thread_id = ?",
                (owner, thread_id),
            )
            await db.commit()

    @staticmethod
    def _row_to_session(row) -> SessionContext:
        return SessionContext(
            thread_id=row["thread_id"],
            topic=row["topic"] or "",
            status=row["status"] or "active",
            participants=set(json.loads(row["participants"] or "[]")),
            history_ids=json.loads(row["history_ids"] or "[]"),
            blocked_reason=row["blocked_reason"],
            last_activity=datetime.fromisoformat(row["last_activity"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            summary=row["summary"] or "",
        )
