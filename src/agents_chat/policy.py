"""
Network Policy: 全网络流量控制 + 自由聊天机制.

解决问题 1 (流量控制):
- Per-author mail rate limit (每小时最多 N 封)
- Per-author action rate (每 tick 最多 N 个 action)
- Per-thread max rounds (防止 Re: 死循环)
- Global token budget (可选, 烧钱监控)

解决问题 2 (自由聊天):
- FreeChatTrigger: 周期触发自由讨论
- 自由聊天有自己的短会话 (10 轮上限)
- 所有人 burst tick 参与, 但鼓励短回话
"""

from __future__ import annotations

import json
import time
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


@dataclass
class NetworkPolicy:
    """网络级 policy. 全局一套."""

    # Per-author 邮件限制
    max_mails_per_hour: int = 30          # 每小时最多发 30 封
    max_mails_per_day: int = 200          # 每天最多 200 封 (跟 OpenRouter 对齐)

    # Per-tick 限制
    max_actions_per_tick: int = 3         # 一次 tick 最多 3 个 action (防爆炸)

    # Per-thread 限制
    max_thread_rounds: int = 8            # 同一 thread 最多 8 轮 Re: (防死循环)
    thread_idle_close_seconds: int = 600  # 10 分钟无活动自动 close

    # Tick cooldown
    min_tick_interval_seconds: int = 3     # 同一 author 两次 tick 至少 3 秒

    # Free chat
    free_chat_min_authors: int = 2         # 至少 2 个 author 触发自由聊天
    free_chat_max_rounds: int = 10         # 自由聊天最多 10 轮
    free_chat_idle_seconds: int = 120      # 60s 无新消息 → 自动结束

    def to_dict(self) -> dict:
        return asdict(self)


class RateLimiter:
    """Per-author 流量控制.

    用 in-memory counter + SQLite 持久化.
    按小时/天分桶.
    """

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
                    period TEXT NOT NULL,         -- "hour:2026-06-06T12" 或 "day:2026-06-06"
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
        """读某 owner 的当前周期计数."""
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
        """+n, 返回新计数."""
        with self._lock:
            import sqlite3
            p_h = self._current_hour()
            p_d = self._current_day()
            with sqlite3.connect(self.db_path) as db:
                # hour
                db.execute("""
                    INSERT INTO rate_counters (owner, period, count) VALUES (?, ?, ?)
                    ON CONFLICT(owner, period) DO UPDATE SET count = count + ?
                """, (owner, p_h, n, n))
                # day
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
        """检查 owner 是否可以发邮件. 返回 (ok, reason)."""
        h = self.get_count(owner, "hour")
        d = self.get_count(owner, "day")
        if h >= max_per_hour:
            return False, f"hourly limit: {h}/{max_per_hour}"
        if d >= max_per_day:
            return False, f"daily limit: {d}/{max_per_day}"
        return True, "ok"


# ============================================================================
# Free Chat
# ============================================================================


@dataclass
class FreeChatSession:
    """一个自由聊天会话."""
    id: str
    topic: str
    started_by: str                # 谁触发的
    started_at: str
    participants: list[str]        # 参与的 authors
    current_round: int = 0
    last_activity_at: str = ""
    status: str = "active"         # "active" | "ended"
    messages: list[dict] = field(default_factory=list)  # in-chat messages

    def to_dict(self) -> dict:
        return asdict(self)


class FreeChatManager:
    """管理自由聊天会话.

    一个 active session, 当 N 轮内没人接话, 自动 end.
    """

    def __init__(self, policy: NetworkPolicy | None = None):
        self.policy = policy or NetworkPolicy()
        self.active: FreeChatSession | None = None
        self._lock = threading.Lock()

    def trigger(
        self,
        topic: str,
        started_by: str,
        authors: list[str],
    ) -> FreeChatSession:
        """开始一个 free chat session. 如果已有 active session, 用新主题替换."""
        with self._lock:
            now = datetime.now().isoformat()
            self.active = FreeChatSession(
                id=str(uuid.uuid4())[:8],
                topic=topic,
                started_by=started_by,
                started_at=now,
                last_activity_at=now,
                participants=list(authors),
            )
            return self.active

    def record_message(self, author: str, body: str) -> bool:
        """记录一则在 free chat 里的消息, 返回是否仍在 active."""
        with self._lock:
            if not self.active or self.active.status != "active":
                return False
            self.active.current_round += 1
            self.active.last_activity_at = datetime.now().isoformat()
            self.active.messages.append({
                "author": author,
                "body": body[:500],
                "ts": self.active.last_activity_at,
            })
            # 轮数上限
            if self.active.current_round >= self.policy.free_chat_max_rounds:
                self.active.status = "ended"
                return False
            return True

    def check_idle(self) -> bool:
        """检查是否 idle, 如果是, 结束. 返回 (was_active, ended)."""
        with self._lock:
            if not self.active or self.active.status != "active":
                return False
            last = datetime.fromisoformat(self.active.last_activity_at)
            if (datetime.now() - last).total_seconds() > self.policy.free_chat_idle_seconds:
                self.active.status = "ended"
                return True
            return False

    def end(self) -> None:
        with self._lock:
            if self.active:
                self.active.status = "ended"

    def get_active(self) -> FreeChatSession | None:
        return self.active

    def to_dict(self) -> dict:
        if not self.active:
            return {"active": False}
        return {"active": True, "session": self.active.to_dict()}
