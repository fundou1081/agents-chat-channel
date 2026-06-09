"""
Agent for v2.0 — 1 worker = 4 组件容器 (Perceive-Decide-Remember-Act).

4 组件 (每个 worker 各 1 套):
  - CommunicationComponent: 感知 (主动+被动) + API 判断
  - EventHandler:           决策 (听 comms, 调 sessions + cli, 写频道)
  - SessionManager:         记忆 (session_id / topic / content / progress)
  - CLI:                    执行 (opencode / qwen / mock)

容器 (Agent class) 只负责:
  - 组装 4 组件 (传对的文件路径 / workspace)
  - 写 <cli_name>.md 引导 (workspace)
  - 委托 main loop 到 event_handler
  - 保留 channel() / mailbox_of() 辅助 (向后兼容)

用法 (跟 v1.x 兼容):
    cli = MockCLI()
    agent = Agent(agent_id="qwencode", cli=cli, data_dir="./data_v2")
    asyncio.create_task(agent.run())  # 后台跑
    agent.stop()                       # 停

内部组件访问:
    agent.comms          # CommunicationComponent
    agent.sessions       # SessionManager
    agent.cli            # CLI
    agent.event_handler  # EventHandler
    agent.mailbox        # Mailbox (this agent's own)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..core.event_handler import EventHandler, extract_mentions
from ..infra.cli.base import CLI
from ..core.communication import CommunicationComponent
from ..core.decision import DecisionConfig, DecisionMaker
from ..infra.files.channel import Channel
from ..infra.files.mailbox import Mailbox
from ..infra.gates import Gate
from ..core.session_manager import SessionManager
from ..infra.state_board import StateBoard


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
        input_gates: list[Gate] | None = None,
        output_gates: list[Gate] | None = None,
        subscriptions: list[str] | None = None,
        mode: str = "passive",
        decision_config: DecisionConfig | None = None,
        decision_maker: DecisionMaker | None = None,
    ):
        self.agent_id = agent_id
        self.cli = cli
        self.data_dir = Path(data_dir)
        self.capabilities = capabilities or []
        self.poll_interval = poll_interval
        self.default_channel = default_channel
        self.system_prompt = system_prompt
        self.input_gates = input_gates
        self.output_gates = output_gates
        self.decision_config = decision_config
        # decision_maker 优先于 decision_config (只有传入非 None 才覆盖)
        if decision_maker is not None:
            self.decision_maker = decision_maker
        elif decision_config:
            self.decision_maker = DecisionMaker(decision_config)
        else:
            self.decision_maker = None
        self.subscriptions = set(subscriptions or [])
        self.mode = mode  # "passive" | "proactive"
        # 主动模式下 poll_interval 稍大 (主动轮询不需要那么频繁)
        if mode == "proactive" and poll_interval < 3.0:
            poll_interval = max(poll_interval, 3.0)

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

        # 4. 决策: EventHandler
        # 内部存 self.event_handler (新名, 准确表达职责)
        # 外部仍可用 self.scheduler (向后兼容 alias)
        self.event_handler = EventHandler(
            comms=self.comms,
            sessions=self.sessions,
            cli=self.cli,
            agent_id=agent_id,
            system_prompt=system_prompt,
            workspace_dir=self.workspace_dir,
            default_channel=default_channel,
            channels_dir=self.channels_dir,
            lock_dir=self.lock_dir,
            input_gates=self.input_gates,
            output_gates=self.output_gates,
            decision_maker=self.decision_maker,
            decision_config=self.decision_config,
        )
        # 向后兼容: 老代码用 agent.scheduler 也可访问
        self.scheduler = self.event_handler

    # ============ 公共 API ============

    def stop(self):
        """停整个 agent: 设 stop 事件 + 取消 run task."""
        self.comms.stop()
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            print(f"[{self.agent_id}] task cancel requested")

    # ============ 订阅管理 (主动模式) ============

    def add_subscription(self, channel: str) -> None:
        """动态订阅频道 (运行时可调)."""
        self.subscriptions.add(channel)
        self.event_handler.add_subscription(channel)

    def remove_subscription(self, channel: str) -> None:
        """取消订阅频道."""
        self.subscriptions.discard(channel)
        self.event_handler.remove_subscription(channel)

    def list_subscriptions(self) -> set[str]:
        """返回当前订阅列表."""
        return set(self.subscriptions)

    async def run(self):
        """主循环. 同时处理被动和主动模式."""
        print(f"[{self.agent_id}] ▶ run (subscriptions={list(self.subscriptions)})")
        self._run_task = asyncio.current_task()
        try:
            # 如果有订阅，启动主动轮询
            if self.subscriptions:
                for ch in self.subscriptions:
                    self.event_handler.add_subscription(ch)
                # 后台运行 proactive 轮询
                asyncio.create_task(self.event_handler.run_proactive())
            
            # 主循环：监听邮箱事件（被动模式）
            await self.event_handler.run()
        except asyncio.CancelledError:
            print(f"[{self.agent_id}] run cancelled")

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
        """构造 {cli_name}.md 引导文件内容.

        基于用户分享的 Claude Code AGENTS.md 模板 (图 5, 12):
          模板格式: 你是 {agent_name}。{role}。你在一个多人协作频道中。
                    频道：#{channel}，成员：{members}。

        加 5 条铁律 (Claude Code 经验):
          1. 开头 @名字
          2. 不确定就 @频道管理员
          3. 管理员指令立即执行
          4. 角色扮演中继续演, 不退出
          5. [STATUS] 简述 | 下一步: xxx (单行格式)
        """
        ch = self.channel(self.default_channel)
        members = ch.list_members()
        admins = ch.list_admins()
        members_str = ", ".join(members) if members else "(无)"
        return f"""# {self.agent_id} 角色定义 (v2.0) — 模板自动生成

你是 **{self.agent_id}**。{self.system_prompt or '通用 worker'}。你在一个多人协作频道中。
频道: #{self.default_channel}, 成员: {members_str}。
频道管理员: {', '.join(admins) if admins else '(无)'}。

本文件由 Agent 启动时自动生成, 你可以手动修改, Agent 不会覆盖.
参考 Claude Code AGENTS.md 格式: `templates/system_prompt.md` 自动填充.

## 能力标签

{', '.join(self.capabilities) or '通用'}

## ⚠️ 5 条铁律 (频道通信, Claude Code 经验)

1. **开头 @名字**: 每一条 reply **必须在开头**指定收信人
   - 格式: `@名字 你的内容`
   - 例: `@buyer-fish 80 太贵, 70 卖不卖?`
   - 例: `@seller-fish 成交!`
   - **不**写 `@xxx:` 格式 (会混乱 Scanner mention 路由)

2. **不确定就 @频道管理员**: 不确定对谁说, 就 `@频道管理员`
   - 频道管理员在 metadata `admins` 列表里 (这里: {', '.join(admins) if admins else '(无)'})
   - v2 Scanner 模糊匹配会路由到 admin

3. **管理员指令立即执行**: 频道管理员的指令**立即执行**, 不要先回 "收到/好的"
   - Scheduler 会处理 admin mention (跟普通 mention 一样)
   - 你的 reply 应该直接包含答案/动作, 不用 "OK" 前缀

4. **角色扮演中继续演, 不退出**: 在角色扮演场景里, 收到对方台词**继续演**
   - 例: 讨价还价场景里, buyer 报 70, seller 继续报 80 (不要退出角色)
   - 你的 prompt 会注入真频道历史 (含其他 agent 的真回复) — 用它来续剧情

5. **末尾 [STATUS] 简述 | 下一步: xxx** (单行格式, **不要**多行 HTML):
   - 格式: `[STATUS] <一句话> | 下一步: <action>`
   - 例: `[STATUS] 报价 100 元 | 下一步: 等 buyer 还价`
   - 多字段: `[STATUS] progress=70 confidence=high | 已完成 | 下一步: 提交`
   - 缺 STATUS 块 = 你这条回复 Scheduler 看不到进度 (会触发 request_status 重试)

## 模糊匹配规则 (重要!)

频道里 @mention 你时, Scanner 会做**模糊匹配**:
- 完全匹配: `@seller` -> seller-fish
- prefix: `@sell` -> seller-fish
- 子串: `@fish` -> seller-fish
- 多个匹配: 选**最长**的 candidate (更精确)
- **没匹配**: 邮件不投递, 静默忽略 (返回 None)

**反面例子**: `@bot` 同时是 "robot" 和 "bot-1" 的 prefix, 选更长的 -> "robot"

## 工作环境 (相对本文件)

- 频道: `../channels/{{name}}.jsonl` (JSONL, 可读可写)
- 我的邮箱: `../mailboxes/{self.agent_id}.json` (pending 邮件)
- 我的 sessions: `../sessions/{self.agent_id}.json` (session 列表, 含 topic/progress)
- 任务状态板: `../state_board.json` (全局, 只读)
- 任务锁: `../locks/task_xxx.lock` (5min TTL, mtime 判超时)
- 频道元数据: `../channels/{{name}}.jsonl.meta.json` (含 members + admins)

## 完整工作流 (5 步)

1. 收到 mail (mention / task_broadcast) → Scheduler 决定续/新建 session
2. 构造 prompt (含本文件 + session 上下文 + 频道真历史)
3. 调 CLI ({self.cli.name}) → reply
4. **5 条铁律** 写 reply (开头 @名字 + 末尾 [STATUS])
5. 写频道 + second-route mentions

## 本文件说明

- 文件名: `{md_name}` (跟 CLI 工具名匹配, e.g. claude.md / opencode.md / qwen.md)
- CLI ({self.cli.name}) 启动后会自动读本文件作为角色引导
- 修改本文件不会影响 Agent 行为 (Agent 不读), 但会影响 CLI 行为
- 模板: 跟 Claude Code AGENTS.md 一致 (`templates/system_prompt.md` 自动填充)
"""

    def _format_members(self, members: list[str], admins: list[str]) -> str:
        if not members:
            return "(无 - 频道未声明成员, fallback 到所有 agent)"
        lines = []
        for m in members:
            role = " (admin)" if m in admins else ""
            lines.append(f"- `{m}`{role}")
        return "\n".join(lines)
