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
    """一个频道 (JSONL file + 可选 metadata sidecar).

    Metadata 文件 {name}.meta.json 存成员列表 + admin 列表.
    成员信息独立于 JSONL 消息, 避免每条消息都重复 members 列表.
    """

    def __init__(self, path: str | Path, name: str = ""):
        self.path = Path(path)
        self.name = name or self.path.stem
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # metadata sidecar: channels/{name}.meta.json
        self.meta_path = self.path.with_suffix(self.path.suffix + ".meta.json")
        # touch JSONL
        if not self.path.exists():
            self.path.touch()
        # touch metadata
        if not self.meta_path.exists():
            self._save_meta({"name": self.name, "members": [], "admins": [], "created_by": "", "created_at": ""})

    # ------------------------------------------------------------------
    # Metadata (members / admins)
    # ------------------------------------------------------------------

    def _load_meta(self) -> dict:
        if not self.meta_path.exists():
            return {
                "name": self.name, "members": [], "admins": [],
                "human_admins": [], "admin_types": {},
                "created_by": "", "created_at": "",
            }
        try:
            import json
            data = json.loads(self.meta_path.read_text("utf-8"))
            # 兼容老 metadata: 补字段
            data.setdefault("human_admins", [])
            data.setdefault("admin_types", {})
            data.setdefault("members", [])
            data.setdefault("admins", [])
            return data
        except (json.JSONDecodeError, OSError):
            return {
                "name": self.name, "members": [], "admins": [],
                "human_admins": [], "admin_types": {},
                "created_by": "", "created_at": "",
            }

    def _save_meta(self, meta: dict):
        import json, os, tempfile
        fd, tmp = tempfile.mkstemp(
            dir=str(self.meta_path.parent),
            prefix=f".{self.meta_path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.meta_path)
        except Exception:
            try: os.unlink(tmp)
            except OSError: pass
            raise

    def add_member(self, agent_id: str) -> bool:
        """加成员. 返回 True=新增, False=已存在."""
        meta = self._load_meta()
        if agent_id in meta.get("members", []):
            return False
        meta.setdefault("members", []).append(agent_id)
        self._save_meta(meta)
        return True

    def add_admin(self, agent_id: str, is_worker: bool = True) -> bool:
        """加管理员.

        参数:
          - agent_id: 管理员 id
          - is_worker: True=worker agent (有 mailbox, scanner 会投递)
                       False=人类 admin (没 mailbox, 写在 human_admins 列表,
                            频道里看, 不会被 scanner 误投递)

        兼容性: 老调用 `add_admin(agent_id)` 默认 is_worker=True,
                行为跟以前一致.
        """
        meta = self._load_meta()
        members = meta.setdefault("members", [])
        admins = meta.setdefault("admins", [])
        human_admins = meta.setdefault("human_admins", [])
        admin_types = meta.setdefault("admin_types", {})

        if is_worker:
            # 已经在 admins 里
            if agent_id in admins:
                return False
            admins.append(agent_id)
            admin_types[agent_id] = "worker"
            if agent_id not in members:
                members.append(agent_id)
        else:
            # 人类 admin: 写到 human_admins, 不进 admins
            if agent_id in human_admins:
                return False
            human_admins.append(agent_id)
            admin_types[agent_id] = "human"
        self._save_meta(meta)
        return True

    def list_members(self) -> list[str]:
        return list(self._load_meta().get("members", []))

    def list_admins(self) -> list[str]:
        """返回 worker admins (跟老 API 兼容).

        人类 admin 在 list_human_admins() 里.
        """
        return list(self._load_meta().get("admins", []))

    def list_human_admins(self) -> list[str]:
        """返回人类 admins (新). scanner 不会投递到这些."""
        return list(self._load_meta().get("human_admins", []))

    def is_admin(self, agent_id: str, is_worker: bool | None = None) -> bool:
        """检查 agent_id 是不是 admin.

        参数:
          - is_worker=None: 任意类型 (worker 或 human)
          - is_worker=True: 只看 worker admins
          - is_worker=False: 只看 human admins
        """
        meta = self._load_meta()
        if is_worker is None:
            return agent_id in meta.get("admins", []) or agent_id in meta.get("human_admins", [])
        if is_worker:
            return agent_id in meta.get("admins", [])
        return agent_id in meta.get("human_admins", [])

    def remove_admin(self, agent_id: str, is_worker: bool = True) -> bool:
        """移除 admin."""
        meta = self._load_meta()
        if is_worker:
            if agent_id in meta.get("admins", []):
                meta["admins"].remove(agent_id)
                meta.get("admin_types", {}).pop(agent_id, None)
                self._save_meta(meta)
                return True
        else:
            if agent_id in meta.get("human_admins", []):
                meta["human_admins"].remove(agent_id)
                meta.get("admin_types", {}).pop(agent_id, None)
                self._save_meta(meta)
                return True
        return False

    def is_member(self, agent_id: str) -> bool:
        return agent_id in self.list_members()

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
