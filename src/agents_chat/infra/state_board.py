"""
State board for v2.0 — 全局任务状态板 (设计文档 9.3).

JSON 文件, key=task_id, value=task status dict.

{
  "task_042": {
    "agent": "qwencode",
    "session": "local_sess_001",
    "remote_session": "qwen_sess_42",
    "task_id": "task_042",
    "progress": 70,
    "summary": "...",
    "next_action": "...",
    "confidence": "high",
    "claimed_at": "2024-06-06T10:00:00Z",
    "heartbeat": "2024-06-06T10:15:00Z",
    "channel": "general",
    "ref_msg_id": "ch_general_100"
  }
}

写入方: Scanner 解析 STATUS 块后 upsert.
读取方: Scheduler 扫超时 / 调度决策 / Web 面板.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateBoard:
    """全局任务状态板."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.path.exists():
            self._write_unlocked({})

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            data = self._read_unlocked()
            # 兼容 {"tasks": {...}} 格式
            tasks = data.get("tasks", data) if isinstance(data, dict) else {}
            entry = tasks.get(task_id)
            return dict(entry) if isinstance(entry, dict) else None

    def list_all(self) -> dict[str, dict]:
        with self._lock:
            data = self._read_unlocked()
            tasks = data.get("tasks", data) if isinstance(data, dict) else {}
            return {k: dict(v) for k, v in tasks.items() if isinstance(v, dict)}

    def list_by_agent(self, agent_id: str) -> dict[str, dict]:
        with self._lock:
            data = self._read_unlocked()
            # 兼容 {"tasks": {...}} 和直接 {...} 格式
            tasks = data.get("tasks", data) if isinstance(data, dict) else {}
            return {k: dict(v) for k, v in tasks.items() if isinstance(v, dict) and v.get("agent") == agent_id}

    def list_stale(self, ttl_seconds: int) -> dict[str, dict]:
        """返回所有 heartbeat 超过 ttl 的 task (Scheduler 用)."""
        from datetime import datetime
        now = datetime.now(timezone.utc)
        with self._lock:
            data = self._read_unlocked()
            tasks = data.get("tasks", data) if isinstance(data, dict) else {}
            stale = {}
            for tid, entry in tasks.items():
                if not isinstance(entry, dict):
                    continue
                hb_str = entry.get("heartbeat", "")
                if not hb_str:
                    stale[tid] = dict(entry)
                    continue
                try:
                    hb = datetime.fromisoformat(hb_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if (now - hb).total_seconds() > ttl_seconds:
                    stale[tid] = dict(entry)
            return stale

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def claim(
        self,
        task_id: str,
        agent_id: str,
        session_local: str,
        channel: str = "",
        ref_msg_id: str = "",
        remote_session: str = "",
    ) -> dict:
        """Agent 认领 task. 创建 entry, status=claimed, heartbeat=now."""
        entry = {
            "agent": agent_id,
            "session": session_local,
            "remote_session": remote_session,
            "task_id": task_id,
            "progress": 0,
            "summary": "claimed",
            "next_action": "",
            "confidence": "medium",
            "claimed_at": _now_iso(),
            "heartbeat": _now_iso(),
            "channel": channel,
            "ref_msg_id": ref_msg_id,
        }
        with self._lock:
            data = self._read_unlocked()
            data["tasks"][task_id] = entry
            self._write_unlocked(data)
        return entry

    def update_from_status(self, task_id: str, status: dict, agent_id: str = "") -> bool:
        """Scanner 解析 STATUS 块后调用, 合并 status 字段.

        保留: agent / session / remote_session / claimed_at / channel / ref_msg_id
        更新: progress / summary / next_action / confidence / heartbeat
        """
        with self._lock:
            data = self._read_unlocked()
            # 确保 tasks 容器存在
            if "tasks" not in data or not isinstance(data.get("tasks"), dict):
                data["tasks"] = {}
            tasks = data["tasks"]
            
            entry = tasks.get(task_id)
            if not entry:
                # 不存在则创建
                entry = {
                    "agent": agent_id or "unknown",
                    "session": "",
                    "remote_session": "",
                    "task_id": task_id,
                    "claimed_at": _now_iso(),
                    "channel": "",
                    "ref_msg_id": "",
                }
                tasks[task_id] = entry
            # 更新可覆盖字段
            for k in ("progress", "summary", "next_action", "confidence"):
                if k in status and status[k] is not None:
                    entry[k] = status[k]
            entry["heartbeat"] = _now_iso()
            if agent_id and not entry.get("agent"):
                entry["agent"] = agent_id
            self._write_unlocked(data)
        return True

    def touch_heartbeat(self, task_id: str) -> bool:
        with self._lock:
            data = self._read_unlocked()
            tasks = data.get("tasks", {})
            if task_id in tasks:
                tasks[task_id]["heartbeat"] = _now_iso()
                self._write_unlocked(data)
                return True
        return False

    def release(self, task_id: str) -> bool:
        """释放 task (锁超时 / 任务完成 / 重新分配)."""
        with self._lock:
            data = self._read_unlocked()
            tasks = data.get("tasks", {})
            if task_id in tasks:
                del tasks[task_id]
                self._write_unlocked(data)
                return True
        return False

    def complete(self, task_id: str) -> bool:
        """标记 task 完成 (progress=100), 但保留 entry 一段时间供查阅."""
        with self._lock:
            data = self._read_unlocked()
            tasks = data.get("tasks", {})
            if task_id in tasks:
                tasks[task_id]["progress"] = 100
                tasks[task_id]["heartbeat"] = _now_iso()
                tasks[task_id]["completed_at"] = _now_iso()
                self._write_unlocked(data)
                return True
        return False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {"tasks": {}}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            # 统一格式: 确保返回 {"tasks": {...}}
            if "tasks" not in data:
                # 旧格式: {task_id: entry, ...} → {"tasks": {task_id: entry}}
                # 跳过非 dict 值 (updated_at 等元字段)
                tasks = {k: v for k, v in data.items() if isinstance(v, dict)}
                data = {"tasks": tasks}
            return data
        except (json.JSONDecodeError, OSError):
            return {"tasks": {}}

    def _write_unlocked(self, data: dict):
        # 确保统一格式
        if "tasks" not in data:
            tasks = {k: v for k, v in data.items() if isinstance(v, dict)}
            data = {"tasks": tasks, "updated_at": _now_iso()}
        elif "updated_at" not in data:
            data["updated_at"] = _now_iso()
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
