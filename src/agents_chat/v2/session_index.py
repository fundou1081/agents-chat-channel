"""
Session index for v2.0.

每个 Agent 一个 JSON 文件, 维护 local_sess → remote_sess 映射.
设计文档 8.1:

{
  "sessions": {
    "local_sess_001": {
      "remote_session_id": "qwen_sess_42",
      "topic": "...",
      "last_active": "2024-06-06T10:00:00Z",
      "channel": "ch1"
    }
  }
}

Agent 处理邮件时:
  1. 收到 task 邮件, 提取 context_hint (local_sess_id) 或 topic keywords
  2. 匹配已有 session: 找到 → 用 remote_session_id resume
  3. 没匹配: 新建 local_sess_xxx, CLI 第一次调用 → 拿 remote_sess_id → 记录映射
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SessionIndex:
    """一个 Agent 的 session 索引."""

    def __init__(self, path: str | Path, agent_id: str = ""):
        self.path = Path(path)
        self.agent_id = agent_id or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write_unlocked({"agent": self.agent_id, "sessions": {}})

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, local_id: str) -> Optional[dict]:
        with self._lock:
            data = self._read_unlocked()
            return data.get("sessions", {}).get(local_id)

    def get_remote(self, local_id: str) -> Optional[str]:
        sess = self.get(local_id)
        if not sess:
            return None
        rid = sess.get("remote_session_id", "")
        return rid if rid else None

    def list_all(self) -> dict[str, dict]:
        with self._lock:
            data = self._read_unlocked()
            return dict(data.get("sessions", {}))

    def find_by_topic(self, topic_keyword: str) -> Optional[str]:
        """按 topic 关键词模糊匹配. 返回 local_id 或 None."""
        if not topic_keyword:
            return None
        kw = topic_keyword.lower()
        with self._lock:
            data = self._read_unlocked()
            for local_id, sess in data.get("sessions", {}).items():
                if kw in sess.get("topic", "").lower():
                    return local_id
        return None

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def create(
        self,
        topic: str = "",
        channel: str = "",
        local_id: str = "",
        remote_id: str = "",
    ) -> str:
        """新建 session. 返回 local_id."""
        if not local_id:
            local_id = f"local_{self.agent_id}_{uuid.uuid4().hex[:6]}"
        with self._lock:
            data = self._read_unlocked()
            data.setdefault("sessions", {})[local_id] = {
                "remote_session_id": remote_id,
                "topic": topic,
                "last_active": _now_iso(),
                "channel": channel,
            }
            self._write_unlocked(data)
        return local_id

    def set_remote(self, local_id: str, remote_id: str) -> bool:
        """更新 remote_session_id (CLI 第一次调用后)."""
        with self._lock:
            data = self._read_unlocked()
            sess = data.get("sessions", {}).get(local_id)
            if not sess:
                return False
            sess["remote_session_id"] = remote_id
            sess["last_active"] = _now_iso()
            self._write_unlocked(data)
        return True

    def touch(self, local_id: str) -> bool:
        """更新 last_active."""
        with self._lock:
            data = self._read_unlocked()
            sess = data.get("sessions", {}).get(local_id)
            if not sess:
                return False
            sess["last_active"] = _now_iso()
            self._write_unlocked(data)
        return True

    def remove(self, local_id: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            if local_id in data.get("sessions", {}):
                del data["sessions"][local_id]
                self._write_unlocked(data)
                return True
        return False

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
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
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
