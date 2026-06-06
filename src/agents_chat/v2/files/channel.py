"""
Channel file (JSONL) for v2.0.

每个频道一个 .jsonl 文件, 每行一条消息. 追加用 'a' 模式, POSIX
保证 < PIPE_BUF (4096) 的 write 原子.

消息 schema (v2.0 设计文档):
{
  "id": "ch_general_100",      # 全局唯一
  "ts": "2026-06-06T22:30:00Z",
  "from": "qwencode",          # sender
  "content": "@claude 看一下",
  "mentions": ["claude"],
  "type": "mention",           # mention | task_broadcast | reply | system | status_report
  "ref_msg_id": "ch_general_099",   # 可选, 引用原消息
  "task_id": "task_042"        # 可选, 关联任务
}
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


# PIPE_BUF on POSIX = 4096. JSONL 行 < 4K 保证原子追加.
_ATOMIC_WRITE_LIMIT = 4096


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Channel:
    """一个频道 (JSONL file)."""

    def __init__(self, path: str | Path, name: str = ""):
        self.path = Path(path)
        self.name = name or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # touch 一下确保文件存在
        if not self.path.exists():
            self.path.touch()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def append(
        self,
        from_: str,
        content: str,
        type: str = "mention",
        mentions: list[str] | None = None,
        ref_msg_id: str = "",
        task_id: str = "",
        msg_id: str = "",
    ) -> str:
        """追加一条消息. 返回生成的 msg_id.

        自动:
          - msg_id: ch_{name}_{counter} (从文件现有行数算)
          - ts: 当前 UTC ISO
        """
        mentions = mentions or []
        if not msg_id:
            # 简单 counter: 基于文件行数
            line_no = self._count_lines()
            msg_id = f"ch_{self.name}_{line_no + 1}"

        msg = {
            "id": msg_id,
            "ts": _now_iso(),
            "from": from_,
            "content": content,
            "mentions": mentions,
            "type": type,
        }
        if ref_msg_id:
            msg["ref_msg_id"] = ref_msg_id
        if task_id:
            msg["task_id"] = task_id

        line = json.dumps(msg, ensure_ascii=False)
        # POSIX atomic guarantee when < PIPE_BUF
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            if len(line) + 1 > _ATOMIC_WRITE_LIMIT:
                # 超过 4K 警告 (不阻断, 因为是 file 不是 pipe)
                pass
        return msg_id

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def tail(self, n: int = 50) -> list[dict]:
        """最后 n 条消息 (返回顺序: 旧 → 新)."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return []
        # 简单实现: 全读后 slice. v2.0 频道文件不会太大.
        with open(self.path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        msgs = [json.loads(l) for l in lines[-n:] if l.strip()]
        return msgs

    def read_since(self, offset: int = 0) -> tuple[list[dict], int]:
        """从第 offset 行开始读, 返回 (messages, new_offset).

        用于 Scanner 增量扫描.
        """
        if not self.path.exists():
            return [], 0
        with open(self.path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()
        new_offset = len(all_lines)
        msgs = []
        for line in all_lines[offset:]:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 跳过损坏行
        return msgs, new_offset

    def iter_all(self) -> Iterator[dict]:
        """迭代全部消息 (generator)."""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _count_lines(self) -> int:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return 0
        with open(self.path, "rb") as f:
            return sum(1 for _ in f)

    def __len__(self) -> int:
        return self._count_lines()
