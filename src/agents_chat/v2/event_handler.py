"""
EventHandler for v2.0 — 1 个 agent 的事件处理器 (decision pipeline).

职责 (Decide orchestrator):
  听 comms 事件, 按 pipeline 处理每种事件:
    - mail 事件: GATE → DECIDE → SESSION → BUILD_PROMPT → CLI → GATE → WRITE
    - stale_task: 调 LLM 重新生成 STATUS 块 (heartbeat)
    - active_task: 同 mail 流程 (启动时扫到)

输入: comms.listen() 的 (event_type, event_data)
输出: 调 sessions + cli + channel.write

为什么叫 EventHandler (不是 Scheduler):
  - "Scheduler" 容易误解为 "定时间调度", 实际是 "按事件跑 pipeline"
  - "EventHandler" 准确表达: 接到事件 → 处理事件
  - 内部不调 LLM (LLM 决策交给 DecisionMaker, LLM 生成交给 CLI)
  - 内部不存 session (SessionManager 的事)
  - 内部不感知 channel/mailbox 路由 (Scanner 的事)
  - 纯 orchestrator: 串联 6 个步骤, 每步失败有 fallback

跟其他 3 组件的关系:
  - comms (感知): 给我事件
  - sessions (记忆): 我读/写
  - cli (执行): 我调
  - 自身: 决策 pipeline (gate / decide / session / build_prompt / cli / gate / write)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .cli.base import CLI
from .communication import CommunicationComponent
from .decision import DecisionConfig, DecisionMaker
from .gates import Gate, GateChain, GateResult
from .files.channel import Channel
from .files.lock import (
    acquire as lock_acquire,
    is_held_by as lock_is_held_by,
    refresh as lock_refresh,
    release as lock_release,
)
from .files.mailbox import Mailbox
from .session_manager import Session, SessionManager
from .state_board import StateBoard
from .status import parse_status_block


logger = logging.getLogger(__name__)


# mention 提取
_MENTION_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_-]*)")
# task 标记
_TASK_TAG_RE = re.compile(r"\[TASK(?:\s+(task[_-]\w+))?\]", re.IGNORECASE)
# 默认锁 TTL
LOCK_TTL_SECONDS = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_mentions(text: str) -> list[str]:
    """从文本提取 @mention 列表 (去重, 保持顺序)."""
    seen = set()
    result = []
    for m in _MENTION_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


def derive_task_id(content: str, ref_msg_id: str = "") -> str:
    """从 content 抓 task_id. 优先 [TASK task_xxx] 标记, 否则 ref."""
    m = _TASK_TAG_RE.search(content or "")
    if m and m.group(1):
        return m.group(1)
    if ref_msg_id:
        return f"task_{ref_msg_id}"
    import hashlib
    h = hashlib.md5((content or "").encode()).hexdigest()[:8]
    return f"task_auto_{h}"


class EventHandler:
    """1 个 agent 的决策大脑. 听 comms 事件, 决定怎么处理.

    跟其他 3 组件的关系:
      - comms (感知): 给我事件
      - sessions (记忆): 我读/写
      - cli (执行): 我调
      - 自身: 决策 (续/新建 session, 解析 STATUS, 写频道)
    """

    def __init__(
        self,
        comms: CommunicationComponent,
        sessions: SessionManager,
        cli: CLI,
        agent_id: str,
        system_prompt: str = "",
        workspace_dir: str | Path | None = None,
        default_channel: str = "general",
        channels_dir: Path | None = None,
        lock_dir: Path | None = None,
        input_gates: list[Gate] | None = None,
        output_gates: list[Gate] | None = None,
        decision_maker: DecisionMaker | None = None,
        decision_config: DecisionConfig | None = None,
    ):
        self.comms = comms
        self.sessions = sessions
        self.cli = cli
        self.agent_id = agent_id
        self.system_prompt = system_prompt
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self.default_channel = default_channel
        self.channels_dir = Path(channels_dir) if channels_dir else None
        self.lock_dir = Path(lock_dir) if lock_dir else None
        # Worker gates: 输入/输出过滤
        self.input_gates = GateChain(list(input_gates or []), direction="input")
        self.output_gates = GateChain(list(output_gates or []), direction="output")
        # DecisionMaker: LLM 决定 session 续/新建/skip
        if decision_maker is not None:
            self.decision_maker = decision_maker
        elif decision_config is not None:
            self.decision_maker = DecisionMaker(decision_config)
        else:
            # 默认: 拿 environment 变量构造 (如果环境没设 api_key, is_ready=False)
            self.decision_maker = DecisionMaker(DecisionConfig())

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self):
        """听 comms 事件, 处理."""
        logger.info(f"[{self.agent_id}] scheduler ▶ run")
        try:
            async for event_type, event_data in self.comms.listen():
                try:
                    if event_type == "mail":
                        await self.handle_mail(event_data)
                    elif event_type == "stale_task":
                        await self.handle_stale_task(event_data)
                    elif event_type == "active_task":
                        # 启动时扫到的 active task: 暂时 noop (等 mail 触发)
                        pass
                except Exception as e:
                    logger.warning(f"[{self.agent_id}] handle {event_type} error: {e}")
                    traceback.print_exc()
        except asyncio.CancelledError:
            pass
        finally:
            logger.info(f"[{self.agent_id}] scheduler ⏹ stopped")

    # ------------------------------------------------------------------
    # 处理 mail
    # ------------------------------------------------------------------

    async def handle_mail(self, mail: dict):
        """处理一封邮件. 核心调度逻辑.

        流程 (7 步 pipeline):
          1. 解析 mail (path / topic / channel / task_id)
          2. INPUT GATE — 过滤 mail.content (拒 → 写 system, return)
          3. **DecisionMaker.decide** — 调 1 次 LLM 决定 continue/new/skip
             - skip → 写 system 消息 "忽略", return
             - continue → 用现有 session
             - new → 新建 session
             - **fallback** (LLM 失败/未配置): SessionManager.decide_session (纯程序化)
          4. 构造 prompt
          5. CLI.execute — 第 2 次 LLM, 生成 reply
          6. OUTPUT GATE — 过滤 reply (拒 → 写 system, return)
          7. 解析 STATUS, 更新 session, 写频道, second-route mentions
        """
        content = mail.get("content", "")
        ref_msg_id = mail.get("ref_msg_id", "")
        channel = mail.get("channel", self.default_channel)
        mail_type = mail.get("type", "mention")
        context_hint = mail.get("context_hint", "")
        task_id = mail.get("task_id") or derive_task_id(content, ref_msg_id)
        # path 来自 Scanner 投递时填的 (Mailbox.append 把 extra.update 到 msg 顶层)
        # email = 显式 @自己, 必答 (二选一)
        # poll/broadcast/system = 其他 (三选一)
        path = mail.get("path", "poll")
        is_must_reply = (path == "email")

        topic = self._extract_topic(content, context_hint)

        # 2. INPUT GATE — 过滤 input (mail.content)
        if self.input_gates:
            input_result = self.input_gates.run(content)
            if not input_result.allowed:
                # gate 拒绝, 写 system 消息到频道, 不调 LLM
                logger.warning(
                    f"[{self.agent_id}] input gate REJECTED: {input_result.reason}"
                )
                await self._write_gate_reject(
                    channel, "input", input_result.reason,
                    ref_msg_id, task_id,
                )
                return
            # gate 改写了 content (sanitize), 更新 mail.content 给 prompt
            if input_result.text != content:
                logger.info(
                    f"[{self.agent_id}] input gate sanitized: {input_result.reason}"
                )
                mail = {**mail, "content": input_result.text}
                content = input_result.text

        # 3. DecisionMaker.decide (1 次 LLM, 决定 session)
        session, is_new = await self._decide_session(mail, task_id, topic, channel, is_must_reply)
        if session is None:
            # skip: 写 system 消息, return
            await self._write_skip(mail, task_id, channel)
            return
        if is_new:
            logger.info(f"[{self.agent_id}] 🆕 new session {session.session_id} for {topic}")
        else:
            logger.info(f"[{self.agent_id}] 🔄 resume session {session.session_id} (progress={session.progress}%)")

        # 4. 构造 prompt
        prompt = self._build_prompt(mail, session, task_id, topic, channel)

        # 5. 调 CLI
        try:
            response = await self.cli.execute(
                session_id=session.remote_id,  # 续; "" = 新建
                prompt=prompt,
                workspace_dir=str(self.workspace_dir) if self.workspace_dir else "",
            )
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CLI exception: {e}")
            await self._write_cli_error(mail, task_id, channel, str(e))
            return

        if not response.ok:
            await self._write_cli_error(mail, task_id, channel, response.error)
            return

        # 6. OUTPUT GATE — 过滤 output (response.output_text)
        output_text = response.output_text
        if self.output_gates:
            output_result = self.output_gates.run(output_text)
            if not output_result.allowed:
                # gate 拒绝, 写 system 消息到频道, 不发 reply
                logger.warning(
                    f"[{self.agent_id}] output gate REJECTED: {output_result.reason}"
                )
                await self._write_gate_reject(
                    channel, "output", output_result.reason,
                    ref_msg_id, task_id,
                )
                return
            if output_result.text != output_text:
                logger.info(
                    f"[{self.agent_id}] output gate sanitized: {output_result.reason}"
                )
                output_text = output_result.text

        # 4. 解析 STATUS
        status = parse_status_block(response.output_text)

        # 5. 更新 session
        try:
            self.sessions.update(
                session.session_id,
                remote_id=response.new_session_id or session.remote_id,
                progress=status.progress if status else session.progress,
                next_action=status.next_action if status else session.next_action,
                content_delta=status.summary if status else None,
                status="completed" if (status and status.progress >= 100) else "active",
                task_id=task_id,
            )
        except Exception as e:
            logger.warning(f"[{self.agent_id}] session update error: {e}")

        # 6. 写频道
        await self._write_channel_reply(channel, output_text, ref_msg_id, task_id)

        # 7. second-route mentions
        await self._second_route(output_text, ref_msg_id, task_id, channel, session.session_id)

    # ------------------------------------------------------------------
    # 辅助: DecisionMaker 集成
    # ------------------------------------------------------------------

    async def _decide_session(
        self, mail: dict, task_id: str, topic: str, channel: str, is_must_reply: bool,
    ) -> tuple[Optional[Session], bool]:
        """调 DecisionMaker 决定 session. 返回 (session, is_new) 或 (None, False) 表示 skip.

        Fallback: DecisionMaker 不可用 / LLM 失败 → SessionManager.decide_session (纯程序化)
        """
        # 1. 准备 sessions snapshot (for DecisionMaker)
        sessions_snapshot = [s.to_dict() for s in self.sessions.list_all()]

        # 2. 调 DecisionMaker (如果 ready)
        if self.decision_maker and self.decision_maker.is_ready:
            try:
                from .decision import Decision
                decision: Decision = await self.decision_maker.decide(
                    mail=mail,
                    sessions=sessions_snapshot,
                    role=self.system_prompt,
                    is_must_reply=is_must_reply,
                )
                logger.info(
                    f"[{self.agent_id}] DecisionMaker: action={decision.action} "
                    f"session={decision.session_id or '-'} reason={decision.reason}"
                )

                # skip: 返回 (None, False) 让 EventHandler 写 system 消息
                if decision.action == "skip":
                    return None, False

                # continue: 用现有 session
                if decision.action == "continue" and decision.session_id:
                    for s in self.sessions.list_all():
                        if s.session_id == decision.session_id:
                            # 关联到当前 task (跟老 decide_session 一致)
                            self.sessions.update(s.session_id, task_id=task_id)
                            return s, False

                # new (或 continue 失败回退): 新建
                new_s = self.sessions.create(
                    topic=topic, channel=channel, task_id=task_id,
                )
                return new_s, True
            except Exception as e:
                logger.warning(
                    f"[{self.agent_id}] DecisionMaker 失败, fallback 到 SessionManager: {e}"
                )
                # 走下面 fallback 路径

        # 3. Fallback: SessionManager.decide_session (纯程序化, 老逻辑)
        session, is_new = self.sessions.decide_session(
            task_id=task_id, topic=topic, channel=channel, session_snapshot=None,
        )
        return session, is_new

    async def _write_skip(self, mail: dict, task_id: str, channel: str):
        """DecisionMaker skip: 写 system 消息到频道, 说明被忽略.

        不调 LLM, 不发 reply. 频道里能审计谁被 skip 了.
        """
        if not self.channels_dir:
            return
        from .files.channel import Channel
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        body = (
            f"[{self.agent_id}] ignored (DecisionMaker skip)\n\n"
            f"<!--STATUS\n"
            f" session_id: -\n"
            f" task_id: {task_id}\n"
            f" progress: 0\n"
            f" summary: {self.agent_id} 决定忽略此 mail\n"
            f" next_action: (无)\n"
            f" confidence: low\n"
            f"-->"
        )
        ch.append(
            from_=self.agent_id, content=body, type="system",
            ref_msg_id=mail.get("ref_msg_id", ""), task_id=task_id,
        )

    # ------------------------------------------------------------------
    # 处理 stale task
    # ------------------------------------------------------------------

    async def handle_stale_task(self, task: dict):
        """stale task: 调 LLM 重新生成 STATUS 块 (heartbeat)."""
        task_id = task.get("task_id", "")
        if not task_id:
            return
        # 找这个 task 的 session
        sessions = self.sessions.list_by_task(task_id)
        if not sessions:
            # task 没关联 session, 写一个 default STATUS 报告
            status = self._default_status_for_task(task)
            await self._write_status_report(task, status)
            return
        session = sessions[0]
        # 调 LLM 重新生成 STATUS
        prompt = f"[scheduler] task {task_id} 已 stale ({self.comms.stale_ttl}s 无 heartbeat), 请更新 STATUS 块"
        try:
            response = await self.cli.execute(
                session_id=session.remote_id,
                prompt=prompt,
                workspace_dir=str(self.workspace_dir) if self.workspace_dir else "",
            )
            if response.ok and response.output_text:
                # 找 STATUS 块
                status = parse_status_block(response.output_text)
                if not status:
                    status = self._default_status_for_task(task)
                await self._write_status_report(task, status)
                # 更新 session
                self.sessions.touch(task_id=task_id) if hasattr(self.sessions, 'touch') else None
        except Exception as e:
            logger.warning(f"[{self.agent_id}] handle_stale_task error: {e}")

    # ------------------------------------------------------------------
    # 辅助: 构造 prompt
    # ------------------------------------------------------------------

    def _build_prompt(
        self, mail: dict, session: Session, task_id: str, topic: str, channel: str,
    ) -> str:
        """构造给 LLM 的 prompt. 包含:
          - Session 上下文 (content_summary / progress / next_action)
          - 频道真实历史 (其他 agent 的真 reply)
          - 当前轮次 (你之前发了几次)
          - 严格输出要求 (禁止剧本 / 禁止模拟对方)
        """
        # 1. 频道真实历史 (最后 10 条)
        recent_msgs = self._read_recent_channel(channel, limit=10)
        history_str = self._format_channel_history(recent_msgs)

        # 2. Session 累积上下文
        session_lines = []
        if session.content_summary:
            session_lines.append(f"[之前内容摘要] {session.content_summary}")
        if session.progress:
            session_lines.append(f"[进度] {session.progress}%")
        if session.next_action:
            session_lines.append(f"[之前 next_action] {session.next_action}")
        session_str = "\n".join(session_lines) if session_lines else "(无, 新 session)"

        # 3. 算自己当前轮次
        my_rounds = sum(1 for m in recent_msgs if m.get("from") == self.agent_id)
        next_round = my_rounds + 1

        prompt = f"""[System]
你是 {self.agent_id}.
{self.system_prompt}

[Session 上下文]
session_id: {session.session_id}
remote_session_id: {session.remote_id or "(未建)"}
topic: {topic}
{session_str}

[频道 {channel} 真实历史 - 按时间顺序, 含其他 agent 的真回复]
{history_str}

[Task]
task_id: {task_id}
mail_type: {mail.get("type", "")}
channel: {channel}
当前轮次: Round {next_round} (这是你作为 {self.agent_id} 的第 {next_round} 轮回复)

[你刚收到的输入]
{mail.get("content", "")}

[输出要求 - 严格!]
1. 只回你自己角色, **1 句话** 报价/还价/接受
2. **禁止** 模拟对方/写剧本/总结整个场景/列历史
3. **禁止** 在你回复里写 "@xxx:" 这种格式 (会混乱)
4. 末尾必须有 STATUS 块:
<!--STATUS
 session_id: {session.session_id}
 task_id: {task_id}
 progress: <0-100>
 summary: <一句话>
 next_action: <下一步>
 confidence: high
-->
"""
        return prompt

    def _read_recent_channel(self, channel: str, limit: int = 10) -> list[dict]:
        """读频道最近 N 条消息. 用 channels_dir 拿 Channel 实例."""
        if not self.channels_dir:
            return []
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        return ch.tail(limit)

    def _format_channel_history(self, msgs: list[dict]) -> str:
        """格式化频道历史给 LLM. 每条截断 200 字符防爆."""
        if not msgs:
            return "(频道空, 你先开口)"
        lines = []
        for m in msgs:
            ts = m.get("ts", "")[:19]
            frm = m.get("from", "?")
            content = m.get("content", "")[:200].replace("\n", " | ")
            lines.append(f"  [{ts}] {frm}: {content}")
        return "\n".join(lines)

    def _extract_topic(self, content: str, hint: str = "") -> str:
        """从 content 抓 topic. 优先 hint, 否则前 30 字符."""
        if hint:
            return hint[:50]
        text = _TASK_TAG_RE.sub("", content or "")
        text = _MENTION_RE.sub("", text)
        text = text.strip().split("\n")[0][:50]
        return text

    # ------------------------------------------------------------------
    # 辅助: 写频道
    # ------------------------------------------------------------------

    async def _write_channel_reply(
        self, channel: str, output_text: str, ref_msg_id: str, task_id: str,
    ):
        """把 LLM 的 reply 写到频道."""
        if not self.channels_dir:
            return
        from .files.channel import Channel
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        mentions = extract_mentions(output_text)
        ch.append(
            from_=self.agent_id,
            content=output_text,
            type="reply",
            mentions=mentions,
            ref_msg_id=ref_msg_id,
            task_id=task_id,
        )

    async def _write_status_report(self, task: dict, status):
        """写 status_report 消息到 task 关联的频道."""
        if not self.channels_dir or not status:
            return
        from .files.channel import Channel
        channel = task.get("channel", self.default_channel)
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        body = (
            f"<!--STATUS\n"
            f" session_id: {status.session_id}\n"
            f" task_id: {status.task_id}\n"
            f" progress: {status.progress}\n"
            f" summary: {status.summary}\n"
            f" next_action: {status.next_action}\n"
            f" confidence: {status.confidence}\n"
            f"-->"
        )
        ch.append(
            from_=self.agent_id,
            content=body,
            type="status_report",
            ref_msg_id=task.get("ref_msg_id", ""),
            task_id=task.get("task_id", ""),
        )

    async def _write_cli_error(
        self, mail: dict, task_id: str, channel: str, error: str,
    ):
        """CLI 失败: 写错误到频道 + STATUS 块."""
        if not self.channels_dir:
            return
        from .files.channel import Channel
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        body = (
            f"[{self.agent_id}] CLI 错误: {error}\n\n"
            f"<!--STATUS\n"
            f" session_id: local_{self.agent_id}\n"
            f" task_id: {task_id}\n"
            f" progress: 0\n"
            f" summary: {self.agent_id} CLI 调用失败\n"
            f" next_action: 等待人工介入\n"
            f" confidence: low\n"
            f"-->"
        )
        ch.append(
            from_=self.agent_id, content=body, type="reply",
            ref_msg_id=mail.get("ref_msg_id", ""), task_id=task_id,
        )

    async def _write_gate_reject(
        self, channel: str, direction: str, reason: str,
        ref_msg_id: str, task_id: str,
    ):
        """Gate 拒绝: 写 system 消息到频道.

        不调 LLM, 不发 reply. 让用户/admin 看到拒绝原因.
        """
        if not self.channels_dir:
            return
        from .files.channel import Channel
        ch = Channel(self.channels_dir / f"{channel}.jsonl", channel)
        body = (
            f"[{self.agent_id}] {direction} gate REJECTED: {reason}\n\n"
            f"<!--STATUS\n"
            f" session_id: local_{self.agent_id}\n"
            f" task_id: {task_id}\n"
            f" progress: 0\n"
            f" summary: {direction} gate 拒绝 ({reason})\n"
            f" next_action: 等待人工调整 (gate 配置或内容)\n"
            f" confidence: low\n"
            f"-->"
        )
        ch.append(
            from_=self.agent_id, content=body, type="system",
            ref_msg_id=ref_msg_id, task_id=task_id,
        )

    # ------------------------------------------------------------------
    # 辅助: second-route
    # ------------------------------------------------------------------

    async def _second_route(
        self, reply_text: str, ref_msg_id: str, task_id: str,
        channel: str, context_hint: str,
    ):
        """提取 reply 里的 @mention, 投递 mention 邮件."""
        if not self.channels_dir:
            return
        from .files.channel import Channel
        from .files.mailbox import Mailbox
        mentions = [m for m in extract_mentions(reply_text) if m != self.agent_id]
        for target in mentions:
            mb_path = self.channels_dir.parent / "mailboxes" / f"{target}.json"
            if not mb_path.exists():
                continue
            mb = Mailbox(mb_path, target)
            mb.append(
                ref_msg_id=ref_msg_id,
                type="mention",
                content=f"@{target} {self.agent_id} 提到你 (task {task_id})",
                channel=channel,
                context_hint=context_hint,
                extra={"task_id": task_id, "from": self.agent_id},
            )

    # ------------------------------------------------------------------
    # 辅助: default status
    # ------------------------------------------------------------------

    def _default_status_for_task(self, task: dict):
        """task 没关联 session, 生成默认 STATUS."""
        from .status import Status
        return Status(
            session_id="",
            task_id=task.get("task_id", ""),
            progress=task.get("progress", 0),
            summary=task.get("summary", "stale"),
            next_action=task.get("next_action", ""),
            confidence=task.get("confidence", "medium"),
        )
