"""
Mailbox file (JSON) for v2.0.

每个 Agent 一个 JSON 文件, pending[] 是待处理邮件.

文件结构 (v2.0 设计文档):
{
  "agent": "qwencode",
  "pending": [
    {
      "ref_msg_id": "ch_general_100",
      "type": "mention",
      "content": "@qwencode 数据库异常",
      "channel": "general",
      "context_hint": "sess_001"   // 可选
    }
  ]
}

操作:
- read_and_clear(): 原子读 + 清空 (read 全量, 写空数组)
- append(msg): Scanner 投递邮件
- peek(): 不清空, 偷看 (调试用)

原子性策略: write 临时文件 -> os.replace() 原子替换.
只在 Agent 端 read_and_clear, Scanner 端只 append (单 writer).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


class Mailbox:
    """一个 Agent 的邮箱."""

    def __init__(self, path: str | Path, agent_id: str = ""):
        self.path = Path(path)
        self.agent_id = agent_id or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 锁: 防止 Agent 端 read_and_clear 跟 Scanner 端 append 竞争
        self._file_lock = threading.Lock()
        # touch + 初始化
        if not self.path.exists():
            self._write_atomic({"agent": self.agent_id, "pending": []})

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def read_and_clear(self) -> list[dict]:
        """原子: 读全部 pending, 然后写空. 返回邮件 list."""
        with self._file_lock:
            data = self._read_unlocked()
            msgs = list(data.get("pending", []))
            self._write_atomic({"agent": data.get("agent", self.agent_id), "pending": []})
            return msgs

    def peek(self) -> list[dict]:
        """不修改, 偷看 pending."""
        with self._file_lock:
            data = self._read_unlocked()
            return list(data.get("pending", []))

    def count(self) -> int:
        """pending 数量."""
        return len(self.peek())

    # ------------------------------------------------------------------
    # 写 (Scanner 投递)
    # ------------------------------------------------------------------

    def append(
        self,
        ref_msg_id: str = "",
        type: str = "mention",
        content: str = "",
        channel: str = "",
        context_hint: str = "",
        extra: dict | None = None,
    ) -> dict:
        """投递一封邮件. 返回投递的邮件 dict.

        type: mention | task_broadcast | opportunity | system_notify | request_status
        """
        msg = {
            "ref_msg_id": ref_msg_id,
            "type": type,
            "content": content,
            "channel": channel,
            "context_hint": context_hint,
            "ts": datetime.utcnow().isoformat() + "Z",
        }
        if extra:
            msg.update(extra)
        with self._file_lock:
            data = self._read_unlocked()
            data.setdefault("pending", []).append(msg)
            self._write_atomic(data)
        return msg

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {"agent": self.agent_id, "pending": []}
        try:
            return json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"agent": self.agent_id, "pending": []}

    def _write_atomic(self, data: dict):
        """原子写: tmp + os.replace."""
        # tmp 文件放在同一目录 (确保 os.replace 原子)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except Exception:
            # 清理 tmp
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
