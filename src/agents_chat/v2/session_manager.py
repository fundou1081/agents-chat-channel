"""
Session + SessionManager for v2.0 — 一个 agent 的"记忆"组件.

跟 v1 SessionIndex 的区别:
  - SessionIndex: 只有 local_id → remote_id 映射 (轻量)
  - SessionManager: 完整 session 状态 (topic / content_summary / progress / next_action)

设计: 1 个 agent 1 个 SessionManager, 自己的文件 (sessions/{agent_id}.json),
原子写 (tmp + os.replace), 线程锁 (跟 Mailbox / StateBoard 一致).

关键 API: decide_session() — 智能决定续/新建
  1. 精确匹配: (channel, task_id) 已存在 → 续
  2. 模糊匹配: 同 channel + topic 关键词匹配 → 续
  3. 不命中 → 新建

用法 (跟 Scheduler 集成):
  session, is_new = sm.decide_session(task_id="t1", topic="鱼市砍价", channel="fish-market")
  sm.update(session.session_id, progress=20, next_action="等 buyer", content_delta="还价 70")
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_internal_id(agent_id: str) -> str:
    return f"local_{agent_id}_{uuid.uuid4().hex[:6]}"


@dataclass
class SessionSnapshot:
    """轻量 session 状态摘要 (Comms / Scanner 调 API 判断时用).

    跟 Session 区别: 不含 remote_id / last_active (API 判断用不上),
    只含判断所需字段 (session_id / topic / progress / next_action / content_summary / status / task_id / channel).

    用法 (Scanner 调 API 判断时, 把所有自己的 session snapshot 一起送):
        my_snapshots = sessions.snapshot()  # list[SessionSnapshot]
        if decide_continue(mail, my_snapshots, ...):
            ...
    """
    session_id: str
    topic: str
    progress: int
    next_action: str
    content_summary: str
    status: str
    task_id: str
    channel: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Session:
    """一个 session (一段连贯工作) 的完整状态.

    字段语义:
      - session_id: 内部 id, 1 个 agent 内唯一
      - remote_id:  LLM session id (qwen_xxx), 用于续
      - topic:      一句话描述这段工作
      - content_summary: 累积的内容摘要 (从 STATUS 块的 summary 字段提取)
      - progress:   0-100, 任务进度
      - next_action: 下一步要做什么
      - status:     active | completed | paused
      - task_id:    关联的 task (供 decide_session 匹配)
      - channel:    频道 (供 decide_session 匹配)
      - last_active: ISO 时间戳
    """
    session_id: str
    remote_id: str = ""
    topic: str = ""
    content_summary: str = ""
    progress: int = 0
    next_action: str = ""
    status: str = "active"  # active | completed | paused
    task_id: str = ""
    channel: str = ""
    last_active: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def snapshot(self) -> "SessionSnapshot":
        """轻量快照 (Comms/Scanner 调 API 判断时用, 避免传整个 Session)."""
        return SessionSnapshot(
            session_id=self.session_id,
            topic=self.topic,
            progress=self.progress,
            next_action=self.next_action,
            content_summary=self.content_summary,
            status=self.status,
            task_id=self.task_id,
            channel=self.channel,
        )


class SessionManager:
    """1 个 agent 的 session 管理器.

    文件: sessions/{agent_id}.json
    结构: {"agent": agent_id, "sessions": {session_id: {...}, ...}}
    """

    def __init__(self, path: str | Path, agent_id: str = ""):
        self.path = Path(path)
        self.agent_id = agent_id or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write_unlocked({"agent": self.agent_id, "sessions": {}})

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create(
        self,
        topic: str,
        channel: str = "",
        task_id: str = "",
        content: str = "",
    ) -> Session:
        """新建 session. 返回 Session."""
        with self._lock:
            data = self._read_unlocked()
            sid = _new_internal_id(self.agent_id)
            s = Session(
                session_id=sid,
                topic=topic,
                channel=channel,
                task_id=task_id,
                content_summary=content,
                last_active=_now_iso(),
            )
            data.setdefault("sessions", {})[sid] = s.to_dict()
            self._write_unlocked(data)
        return s

    def get(self, session_id: str) -> Optional[Session]:
        with self._lock:
            data = self._read_unlocked()
            d = data.get("sessions", {}).get(session_id)
        return Session.from_dict(d) if d else None

    def list_all(self) -> list[Session]:
        with self._lock:
            data = self._read_unlocked()
        return [Session.from_dict(d) for d in data.get("sessions", {}).values()]

    def list_active(self) -> list[Session]:
        return [s for s in self.list_all() if s.status == "active"]

    def list_by_channel(self, channel: str) -> list[Session]:
        return [s for s in self.list_all() if s.channel == channel]

    def list_by_task(self, task_id: str) -> list[Session]:
        """按 task_id 反查 session. 通常 1 个 task 1 个 session, 但可能多个."""
        return [s for s in self.list_all() if s.task_id == task_id]

    def find_by_topic_keyword(self, channel: str, keyword: str) -> Optional[Session]:
        """模糊匹配: channel + topic 含 keyword."""
        for s in self.list_active():
            if s.channel == channel and (keyword in s.topic or s.topic in keyword):
                return s
        return None

    def update(
        self,
        session_id: str,
        progress: Optional[int] = None,
        next_action: Optional[str] = None,
        content_delta: Optional[str] = None,
        status: Optional[str] = None,
        remote_id: Optional[str] = None,
        task_id: Optional[str] = None,
        topic: Optional[str] = None,
    ) -> Optional[Session]:
        """更新 session 字段. 只更新传入的字段."""
        with self._lock:
            data = self._read_unlocked()
            d = data.get("sessions", {}).get(session_id)
            if not d:
                return None
            if progress is not None:
                d["progress"] = max(0, min(100, progress))
            if next_action is not None:
                d["next_action"] = next_action
            if content_delta is not None:
                # 累积模式: 追加到现有 summary
                if d.get("content_summary"):
                    d["content_summary"] = f"{d['content_summary']}; {content_delta}"
                else:
                    d["content_summary"] = content_delta
            if status is not None:
                d["status"] = status
            if remote_id is not None:
                d["remote_id"] = remote_id
            if task_id is not None:
                d["task_id"] = task_id
            if topic is not None:
                d["topic"] = topic
            d["last_active"] = _now_iso()
            data["sessions"][session_id] = d
            self._write_unlocked(data)
        return Session.from_dict(d)

    def remove(self, session_id: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            if session_id in data.get("sessions", {}):
                del data["sessions"][session_id]
                self._write_unlocked(data)
                return True
        return False

    # ------------------------------------------------------------------
    # 关键 API: decide_session — 智能决定续/新建
    # ------------------------------------------------------------------

    def decide_session(
        self,
        task_id: str,
        topic: str,
        channel: str = "",
        session_snapshot: Optional["SessionSnapshot"] = None,
    ) -> tuple[Session, bool]:
        """智能匹配 active session. 返回 (session, is_new).

        规则 (按优先级):
          1. 精确匹配: 同 (channel, task_id) 的 active session → 续
          2. 模糊匹配: 同 channel + topic 关键词 → 续 (考虑 session_snapshot 上下文)
          3. 不命中 → 新建

        参数:
          - session_snapshot: 调 API 时带入的当前 session 状态快照 (可空)
            Scanner 调 decide 时, 把当前正在处理的 session 状态一起送, 让 decide 能考虑
            progress / status 上下文 (例如: 已有 session 进度 >= 100, 不续避免重复触发)
        """
        # 1. 精确匹配
        for s in self.list_active():
            if s.task_id == task_id and (not channel or s.channel == channel):
                return s, False

        # 2. 模糊匹配 (考虑 session_snapshot 上下文)
        for s in self.list_active():
            if s.channel == channel and topic and s.topic:
                if topic in s.topic or s.topic in topic:
                    # 新: 考虑 session_snapshot 判断
                    if session_snapshot:
                        # 如果已有 session 进度 >= 100 (已完成), 不续, 跳过
                        if s.progress >= 100:
                            continue
                    # 续 + 关联到新 task
                    self.update(s.session_id, task_id=task_id)
                    s.task_id = task_id
                    return s, False

        # 3. 新建
        new_s = self.create(topic=topic, channel=channel, task_id=task_id)
        return new_s, True

    def snapshot(self) -> list["SessionSnapshot"]:
        """返回所有 session 的轻量快照列表 (Scanner 调 API 判断时用).

        返回: list[SessionSnapshot] (不含 remote_id / last_active 等冗余字段)
        """
        return [s.snapshot() for s in self.list_all()]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {"agent": self.agent_id, "sessions": {}}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"agent": self.agent_id, "sessions": {}}

    def _write_unlocked(self, data: dict):
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
