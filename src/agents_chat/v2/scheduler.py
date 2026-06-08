"""
Scheduler for v2.0 — 全局调度中心 (设计文档 9.3).

职责 (v2.0 调度 + 锁管理):
  - 定期扫 state_board.list_stale(ttl) → 找出超时 task
  - 第一次超时: 发 request_status 邮件给持有 agent
  - 第二次超时 (request 后仍无响应): 强制释放锁 + 移除 state_board
  - 释放后写频道通知: task_xxx 重新可认领
  - (可选) 依赖解析 / 负载均衡 — 后续 Phase

主循环:
  while not stop:
    check_stale_tasks()
    sleep(check_interval)
"""
from __future__ import annotations

import asyncio
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .files.channel import Channel
from .files.lock import (
    DEFAULT_TTL_SECONDS,
    force_release_if_expired,
)
from .files.mailbox import Mailbox
from .state_board import StateBoard


# 默认超时: 5 分钟 (设计文档 5.2 mailbox poll interval 上限)
DEFAULT_STALE_TTL = 300

# request_status 后再等多久强制释放
DEFAULT_GRACE_PERIOD = 60

# scheduler 自身检查间隔
DEFAULT_CHECK_INTERVAL = 30


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Scheduler:
    """全局调度中心 (后台进程 / 线程)."""

    def __init__(
        self,
        data_dir: str | Path,
        stale_ttl: int = DEFAULT_STALE_TTL,
        grace_period: int = DEFAULT_GRACE_PERIOD,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ):
        self.data_dir = Path(data_dir)
        self.stale_ttl = stale_ttl
        self.grace_period = grace_period
        self.check_interval = check_interval
        self._stop_event = asyncio.Event()

        # 文件 IO
        self.state_board = StateBoard(self.data_dir / "state_board.json")
        self.mailboxes_dir = self.data_dir / "mailboxes"
        self.channels_dir = self.data_dir / "channels"
        self.lock_dir = self.data_dir / "locks"
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # 跟踪: 哪些 task 已经发过 request_status
        # 持久化到 state file (重启续)
        self.request_log_path = self.data_dir / "scheduler_state.json"
        self.request_log: dict[str, str] = self._load_request_log()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def stop(self):
        self._stop_event.set()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def run(self):
        """主循环."""
        print(
            f"[scheduler] ▶ run (stale_ttl={self.stale_ttl}s, "
            f"grace={self.grace_period}s, interval={self.check_interval}s)"
        )
        self._run_task = asyncio.current_task()
        try:
            while not self._stop_event.is_set():
                try:
                    await self._check_once()
                except Exception as e:
                    print(f"[scheduler] ⚠ check error: {e}")
                    traceback.print_exc()
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.check_interval,
                    )
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            print("[scheduler] run cancelled")
        finally:
            print("[scheduler] ⏹ stopped")

    # ------------------------------------------------------------------
    # 核心
    # ------------------------------------------------------------------

    async def _check_once(self):
        """检查一次超时任务."""
        stale = self.state_board.list_stale(self.stale_ttl)
        if not stale:
            return
        print(f"[scheduler] ⏰ {len(stale)} stale task(s) detected")
        for task_id, entry in stale.items():
            await self._handle_stale(task_id, entry)

    async def _handle_stale(self, task_id: str, entry: dict):
        """处理一个 stale task.

        状态机:
          - 第一次发现 stale: 发 request_status, 记录到 request_log
          - grace_period 后仍 stale: 强制释放锁 + 移除 state_board + 写频道
        """
        agent_id = entry.get("agent", "")
        requested_at = self.request_log.get(task_id, "")

        if not requested_at:
            # 第一次: 发 request_status
            await self._request_status(task_id, agent_id, entry)
            self.request_log[task_id] = _now_iso()
            self._save_request_log()
        else:
            # 第二次: 检查是否已超 grace_period
            try:
                req_time = datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
                elapsed = (datetime.now(timezone.utc) - req_time).total_seconds()
            except (ValueError, TypeError):
                elapsed = self.grace_period + 1

            if elapsed >= self.grace_period:
                # 强制清理
                await self._force_release(task_id, agent_id, entry)
                # 清掉 log
                self.request_log.pop(task_id, None)
                self._save_request_log()

    async def _request_status(self, task_id: str, agent_id: str, entry: dict):
        """发 request_status 邮件给持有 agent."""
        if not agent_id:
            return
        mb = self.mailbox_of(agent_id)
        if not mb.path.exists():
            return
        mb.append(
            ref_msg_id=entry.get("ref_msg_id", ""),
            type="request_status",
            content=f"[scheduler] task {task_id} 已 {self.stale_ttl}s 无 heartbeat, 请更新状态",
            channel=entry.get("channel", "general"),
            context_hint=entry.get("session", ""),
            extra={"task_id": task_id},
        )
        # 写频道通知
        self._announce(
            entry.get("channel", "general"),
            f"[scheduler] task {task_id} 状态询问, 等 {self.grace_period}s 后强制释放",
            task_id=task_id,
        )
        print(f"[scheduler] 📨 request_status → {agent_id} for {task_id}")

    async def _force_release(self, task_id: str, agent_id: str, entry: dict):
        """强制释放锁 + 移除 state_board + 写频道."""
        lock_path = self.lock_dir / f"task_{task_id}.lock"
        # 强制释放锁 (即使未过期 — agent 已失联)
        from .files.lock import force_release
        if lock_path.exists():
            force_release(lock_path)
        # 移除 state_board entry
        self.state_board.release(task_id)
        # 写频道
        self._announce(
            entry.get("channel", "general"),
            f"[scheduler] task {task_id} 超时, 锁已释放, 可重新认领 (原 agent: {agent_id})",
            task_id=task_id,
        )
        print(f"[scheduler] 🔓 force-released {task_id} (was held by {agent_id})")

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def mailbox_of(self, agent_id: str) -> Mailbox:
        return Mailbox(self.mailboxes_dir / f"{agent_id}.json", agent_id)

    def channel(self, name: str) -> Channel:
        return Channel(self.channels_dir / f"{name}.jsonl", name)

    def _announce(self, channel_name: str, content: str, task_id: str = ""):
        """写频道 (scheduler 自己的消息)."""
        ch = self.channel(channel_name)
        ch.append(
            from_="scheduler", content=content, type="system",
            task_id=task_id,
        )

    def _load_request_log(self) -> dict[str, str]:
        if not self.request_log_path.exists():
            return {}
        import json
        try:
            data = json.loads(self.request_log_path.read_text("utf-8"))
            return data.get("request_log", {})
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_request_log(self):
        import json
        import os
        import tempfile
        data = {"request_log": self.request_log, "updated_at": _now_iso()}
        tmp = self.request_log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, self.request_log_path)
