"""
Network Policy: 全网络流量控制.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class NetworkPolicy:
    max_mails_per_hour: int = 30
    max_mails_per_day: int = 200
    max_actions_per_tick: int = 3
    max_thread_rounds: int = 8
    thread_idle_close_seconds: int = 600
    min_tick_interval_seconds: int = 3
    free_chat_min_authors: int = 2
    free_chat_max_rounds: int = 10
    free_chat_idle_seconds: int = 120

    def to_dict(self) -> dict:
        return asdict(self)


class RateLimiter:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        import sqlite3
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS rate_counters (
                    owner TEXT NOT NULL,
                    period TEXT NOT NULL,
                    count INTEGER DEFAULT 0,
                    PRIMARY KEY (owner, period)
                );
            """)
            db.commit()

    def _current_hour(self) -> str:
        return f"hour:{datetime.now().strftime('%Y-%m-%dT%H')}"

    def _current_day(self) -> str:
        return f"day:{datetime.now().strftime('%Y-%m-%d')}"

    def get_count(self, owner: str, period: str = "hour") -> int:
        import sqlite3
        p = self._current_hour() if period == "hour" else self._current_day()
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute(
                "SELECT count FROM rate_counters WHERE owner = ? AND period = ?",
                (owner, p),
            )
            row = cursor.fetchone()
        return row[0] if row else 0

    def increment(self, owner: str, n: int = 1) -> int:
        with self._lock:
            import sqlite3
            p_h = self._current_hour()
            p_d = self._current_day()
            with sqlite3.connect(self.db_path) as db:
                db.execute("""
                    INSERT INTO rate_counters (owner, period, count) VALUES (?, ?, ?)
                    ON CONFLICT(owner, period) DO UPDATE SET count = count + ?
                """, (owner, p_h, n, n))
                db.execute("""
                    INSERT INTO rate_counters (owner, period, count) VALUES (?, ?, ?)
                    ON CONFLICT(owner, period) DO UPDATE SET count = count + ?
                """, (owner, p_d, n, n))
                db.commit()
                cursor = db.execute(
                    "SELECT count FROM rate_counters WHERE owner = ? AND period = ?",
                    (owner, p_h),
                )
                row = cursor.fetchone()
            return row[0] if row else n

    def check(self, owner: str, max_per_hour: int, max_per_day: int) -> tuple[bool, str]:
        h = self.get_count(owner, "hour")
        d = self.get_count(owner, "day")
        if h >= max_per_hour:
            return False, f"hourly limit: {h}/{max_per_hour}"
        if d >= max_per_day:
            return False, f"daily limit: {d}/{max_per_day}"
        return True, "ok"


# FreeChatManager 现在集成到 PostsDB (kind=freechat).
# 保留 FreeChatManager 类名作为 wrapper 给旧代码用.
class FreeChatManager:
    def __init__(self, policy: NetworkPolicy | None = None):
        self.policy = policy or NetworkPolicy()

    def trigger(self, topic: str, started_by: str, authors: list[str]) -> dict:
        return {"topic": topic, "started_by": started_by, "authors": authors,
                "active": True, "method": "via-posts-db"}

    def record_message(self, author: str, body: str) -> bool:
        return True

    def check_idle(self) -> bool:
        return False

    def end(self) -> None:
        pass

    def get_active(self):
        return None

    def to_dict(self) -> dict:
        return {"active": False, "method": "FreeChatManager-deprecated-use-posts-db"}
