"""
CommunicationComponent for v2.0 — 1 个 agent 的感知器官.

职责 (Perceive, 主动+被动):
  主动 pull (调 API 拿数据):
    - poll_new_mails()       → mailbox.read_and_clear
    - poll_my_active_tasks() → state_board.list_by_agent
    - poll_stale_tasks()     → state_board.list_stale + 过滤我持有
    - poll_recent_channel()  → channel.read_since

  被动 push (接收事件):
    - on_new_mail()          → Scanner 投递后调这个唤醒
    - on_external_event()    → 通用 push 入口

  简单 API 判断:
    - is_relevant_mail()     → mail 跟我相关吗 (mention / task_broadcast / request_status / system)
    - filter_relevant()      → 批量过滤

  感知循环 (主):
    - listen() async iterator → (event_type, event_data)
      - 主动 poll (周期性)
      - 被动 wait (新 mail event)

跟 Scheduler 集成:
  async for event_type, data in comms.listen():
      scheduler.handle(event_type, data)
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .files.channel import Channel
from .files.mailbox import Mailbox
from .state_board import StateBoard


# poll interval (被动 wait 的 timeout)
DEFAULT_POLL_INTERVAL = 2.0


class CommunicationComponent:
    """1 个 agent 的感知组件. 不调 LLM, 纯程序感知 + 简单判断."""

    def __init__(
        self,
        agent_id: str,
        mailbox: Mailbox,
        channels_dir: Path,
        state_board: StateBoard,
        lock_dir: Path,
        default_channel: str = "general",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        stale_ttl: int = 300,
    ):
        self.agent_id = agent_id
        self.mailbox = mailbox
        self.channels_dir = Path(channels_dir)
        self.state_board = state_board
        self.lock_dir = Path(lock_dir)
        self.default_channel = default_channel
        self.poll_interval = poll_interval
        self.stale_ttl = stale_ttl

        self._stop_event = asyncio.Event()
        self._new_mail_event = asyncio.Event()  # 被动 push

    # ------------------------------------------------------------------
    # 主动 pull (调 API)
    # ------------------------------------------------------------------

    def poll_new_mails(self) -> list[dict]:
        """调 mailbox.read_and_clear API. 同步 (file 操作)."""
        return self.mailbox.read_and_clear()

    def poll_my_active_tasks(self) -> list[dict]:
        """调 state_board.list_by_agent API. 只看我的 task."""
        return list(self.state_board.list_by_agent(self.agent_id).values())

    def poll_stale_tasks(self) -> list[dict]:
        """调 state_board.list_stale + 过滤我持有的."""
        all_stale = self.state_board.list_stale(self.stale_ttl)
        return [
            e for tid, e in all_stale.items()
            if e.get("agent") == self.agent_id
        ]

    def poll_recent_channel(
        self, channel: str = None, since_offset: int = 0,
    ) -> tuple[list[dict], int]:
        """调 channel.read_since API. 返回 (msgs, new_offset)."""
        ch_name = channel or self.default_channel
        ch = self.channel(ch_name)
        return ch.read_since(since_offset)

    def poll_channel_members(self, channel: str) -> list[str]:
        """调 channel.list_members API. 读频道元数据."""
        return self.channel(channel).list_members()

    # ------------------------------------------------------------------
    # 被动 push (接收事件)
    # ------------------------------------------------------------------

    def on_new_mail(self):
        """外部 (Scanner 投递后) 调这个唤醒主动循环."""
        self._new_mail_event.set()

    def on_external_event(self):
        """通用 push 唤醒 (供 Scheduler 触发)."""
        self._new_mail_event.set()

    def stop(self):
        """停止感知循环."""
        self._stop_event.set()
        # 也唤醒让循环退出
        self._new_mail_event.set()

    # ------------------------------------------------------------------
    # 简单 API 判断 (程序化, 不调 LLM)
    # ------------------------------------------------------------------

    def is_relevant_mail(self, mail: dict) -> bool:
        """判断: 这封 mail 跟我相关吗?

        规则:
          - mention / task_broadcast / system_notify: 总是相关 (Scanner 已经路由)
          - request_status: 关联 task 是我持有的 → 相关
          - 其他: 不相关
        """
        mtype = mail.get("type", "")
        if mtype in ("mention", "task_broadcast", "system_notify", "opportunity"):
            return True
        if mtype == "request_status":
            task_id = (
                mail.get("task_id")
                or mail.get("extra", {}).get("task_id", "")
            )
            if not task_id:
                return False
            return task_id in self.state_board.list_by_agent(self.agent_id)
        return False

    def filter_relevant(self, mails: list[dict]) -> list[dict]:
        """批量过滤相关 mail."""
        return [m for m in mails if self.is_relevant_mail(m)]

    def is_my_stale_task(self, task: dict) -> bool:
        """判断: 这个 stale task 是我持有的吗."""
        return task.get("agent") == self.agent_id

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def channel(self, name: str) -> Channel:
        return Channel(self.channels_dir / f"{name}.jsonl", name)

    # ------------------------------------------------------------------
    # 感知循环 (主)
    # ------------------------------------------------------------------

    async def listen(self) -> AsyncIterator[tuple[str, Any]]:
        """持续生成 (event_type, event_data) 给 scheduler.

        yield 事件类型:
          - ("mail", mail_dict):    一封相关 mail
          - ("stale_task", task):    我持有的 stale task
          - ("active_task", task):   我持有的 active task (启动时扫一次)

        退出: 调 stop() 后下一次循环退出.
        """
        # 启动时: 扫一次已有 active task (让 scheduler 处理 stale / 续)
        for task in self.poll_my_active_tasks():
            yield ("active_task", task)

        while not self._stop_event.is_set():
            # 1. 主动 poll
            mails = self.poll_new_mails()
            for m in self.filter_relevant(mails):
                yield ("mail", m)

            for task in self.poll_stale_tasks():
                yield ("stale_task", task)

            # 2. 被动 wait
            try:
                await asyncio.wait_for(
                    self._new_mail_event.wait(), timeout=self.poll_interval,
                )
                self._new_mail_event.clear()
            except asyncio.TimeoutError:
                pass
