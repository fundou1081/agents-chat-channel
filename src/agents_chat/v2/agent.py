"""
Agent for v2.0 — 1 worker = 4 组件容器 (Perceive-Decide-Remember-Act).

4 组件 (每个 worker 各 1 套):
  - CommunicationComponent: 感知 (主动+被动) + API 判断
  - AgentScheduler:         决策 (听 comms, 调 sessions + cli, 写频道)
  - SessionManager:         记忆 (session_id / topic / content / progress)
  - CLI:                    执行 (opencode / qwen / mock)

容器 (Agent class) 只负责:
  - 组装 4 组件 (传对的文件路径 / workspace)
  - 写 <cli_name>.md 引导 (workspace)
  - 委托 main loop 到 scheduler
  - 保留 channel() / mailbox_of() 辅助 (向后兼容)

用法 (跟 v1.x 兼容):
    cli = MockCLI()
    agent = Agent(agent_id="qwencode", cli=cli, data_dir="./data_v2")
    asyncio.create_task(agent.run())  # 后台跑
    agent.stop()                       # 停

内部组件访问:
    agent.comms       # CommunicationComponent
    agent.sessions    # SessionManager
    agent.cli         # CLI
    agent.scheduler   # AgentScheduler
    agent.mailbox     # Mailbox (this agent's own)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agent_scheduler import AgentScheduler, extract_mentions
from .cli.base import CLI
from .communication import CommunicationComponent
from .files.channel import Channel
from .files.mailbox import Mailbox
from .session_manager import SessionManager
from .state_board import StateBoard


# 重新导出 (向后兼容: scanner 等模块从 agent 导入 extract_mentions)
__all__ = ["Agent", "extract_mentions"]


# 锁 TTL
LOCK_TTL_SECONDS = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Agent:
    """1 worker = 4 组件容器."""

    def __init__(
        self,
        agent_id: str,
        cli: CLI,
        data_dir: str | Path,
        capabilities: list[str] | None = None,
        poll_interval: float = 2.0,
        default_channel: str = "general",
        system_prompt: str = "",
        workspace_dir: str | Path | None = None,
    ):
        self.agent_id = agent_id
        self.cli = cli
        self.data_dir = Path(data_dir)
        self.capabilities = capabilities or []
        self.poll_interval = poll_interval
        self.default_channel = default_channel
        self.system_prompt = system_prompt

        # ============ 文件 IO (per agent 自己的) ============
        self.mailbox = Mailbox(
            self.data_dir / "mailboxes" / f"{agent_id}.json", agent_id,
        )
        self.channels_dir = self.data_dir / "channels"
        self.mailboxes_dir = self.data_dir / "mailboxes"
        self.lock_dir = self.data_dir / "locks"
        self.state_board = StateBoard(self.data_dir / "state_board.json")
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # ============ workspace (per agent 独立) ============
        if workspace_dir is None:
            workspace_dir = self.data_dir / "workspaces" / agent_id
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self._init_workspace_files()

        # ============ 4 组件 (组装) ============
        # 1. 记忆: SessionManager
        self.sessions = SessionManager(
            self.data_dir / "sessions" / f"{agent_id}.json", agent_id,
        )

        # 2. 感知: CommunicationComponent
        self.comms = CommunicationComponent(
            agent_id=agent_id,
            mailbox=self.mailbox,
            channels_dir=self.channels_dir,
            state_board=self.state_board,
            lock_dir=self.lock_dir,
            default_channel=default_channel,
            poll_interval=poll_interval,
        )

        # 3. 执行: cli (已经是传入的实例)
        # (self.cli 已设)

        # 4. 决策: AgentScheduler
        self.scheduler = AgentScheduler(
            comms=self.comms,
            sessions=self.sessions,
            cli=self.cli,
            agent_id=agent_id,
            system_prompt=system_prompt,
            workspace_dir=self.workspace_dir,
            default_channel=default_channel,
            channels_dir=self.channels_dir,
            lock_dir=self.lock_dir,
        )

    # ============ 公共 API (向后兼容) ============

    def stop(self):
        """停整个 agent. 委托给 comms 唤醒 scheduler 退出."""
        self.comms.stop()

    async def run(self):
        """主循环. 委派给 scheduler."""
        print(f"[{self.agent_id}] ▶ run (cli={self.cli.name}, components: comms+sessions+cli+scheduler)")
        await self.scheduler.run()

    def channel(self, name: str) -> Channel:
        return Channel(self.channels_dir / f"{name}.jsonl", name)

    def mailbox_of(self, agent_id: str) -> Mailbox:
        return Mailbox(self.mailboxes_dir / f"{agent_id}.json", agent_id)

    def trigger_immediate_tick(self):
        """立刻唤醒感知循环 (向后兼容). 委托给 comms.on_new_mail."""
        self.comms.on_new_mail()

    def snapshot(self) -> dict:
        """快照 (向后兼容: API server / monitor 用)."""
        return {
            "agent_id": self.agent_id,
            "status": "active" if self.comms._stop_event.is_set() is False else "stopped",
            "total_ticks": 0,  # 老 API 兼容, v2 不暴露 tick 计数
            "active_sessions": len(self.sessions.list_active()),
            "mailbox_size": self.mailbox.count(),
        }

    # ============ workspace 引导文件 ============

    def _init_workspace_files(self):
        """写 {workspace_dir}/{cli.name}.md 引导文件 (claude.md / opencode.md / qwen.md).

        CLI 工具启动后会自动读工作目录里这个文件作为角色引导.
        如果文件已存在, 保留 (用户可能手动改了).
        """
        md_name = f"{self.cli.name}.md"
        md_path = self.workspace_dir / md_name
        if md_path.exists():
            return
        md_content = self._build_workspace_md(md_name)
        md_path.write_text(md_content, encoding="utf-8")

    def _build_workspace_md(self, md_name: str) -> str:
        """构造 {cli_name}.md 引导文件内容."""
        ch = self.channel(self.default_channel)
        members = ch.list_members()
        admins = ch.list_admins()
        return f"""# {self.agent_id} 角色定义 (v2.0)

你是 **{self.agent_id}**, 在多 agent 协作网络中工作. 本文件由 Agent 启动时自动生成,
你可以手动修改, Agent 不会覆盖.

## 能力标签

{', '.join(self.capabilities) or '通用'}

## 角色提示 (system_prompt)

{self.system_prompt or '(无)'}

## 默认频道成员 ({self.default_channel})

{self._format_members(members, admins)}

## 模糊匹配规则 (重要!)

频道里 @mention 你时, Scanner 会做**模糊匹配**:
- 完全匹配: `@seller` -> seller-fish
- prefix: `@sell` -> seller-fish
- 子串: `@fish` -> seller-fish
- 多个匹配: 选**最长**的 candidate (更精确)
- **没匹配**: 邮件不投递, 静默忽略

**反面例子**: `@bot` 同时是 "robot" 和 "bot-1" 的 prefix, 选更长的 -> "robot"

## 工作环境 (相对本文件)

- 频道: `../channels/{{name}}.jsonl` (JSONL, 可读可写)
- 我的邮箱: `../mailboxes/{self.agent_id}.json` (pending 邮件)
- 我的 sessions: `../sessions/{self.agent_id}.json` (session 列表, 含 topic/progress)
- 任务状态板: `../state_board.json` (全局, 只读)
- 任务锁: `../locks/task_xxx.lock` (5min TTL, mtime 判超时)
- 频道元数据: `../channels/{{name}}.jsonl.meta.json` (含 members + admins)

## 工作规则

1. **STATUS 块**: 每条 reply 必须嵌入, Scanner 解析用于调度
   ```
   <!--STATUS
    session_id: <local_sess_id>
    task_id: <task_id>
    progress: <0-100>
    summary: <一句话总结>
    next_action: <下一步行动>
    confidence: <low/medium/high>
   -->
   ```
2. **@mention**: reply 里的 `@xxx` 由 Scheduler 提取并投递 mention 邮件
3. **[TASK] 标记**: 频道消息里的 `[TASK task_xxx]` 会被 Scanner 广播给频道**成员**
4. **session resume**: 同一 task + topic 多次处理会自动用同一 session (decide_session 决定)
5. **讨价还价 / 多轮对话**: 用 thread 关联, Scheduler 会路由到原 agent

## 本文件说明

- 文件名: `{md_name}` (跟 CLI 工具名匹配, e.g. claude.md / opencode.md / qwen.md)
- CLI ({self.cli.name}) 启动后会自动读本文件作为角色引导
- 修改本文件不会影响 Agent 行为 (Agent 不读), 但会影响 CLI 行为
"""

    def _format_members(self, members: list[str], admins: list[str]) -> str:
        if not members:
            return "(无 - 频道未声明成员, fallback 到所有 agent)"
        lines = []
        for m in members:
            role = " (admin)" if m in admins else ""
            lines.append(f"- `{m}`{role}")
        return "\n".join(lines)
