"""
Author: long-lived agent with inbox + heartbeat + multi-session.

This is the core abstraction. An Author:
- Has a persistent identity (Persona)
- Has a Mailbox (SQLite-backed)
- Has multiple in-flight Sessions
- Runs a Heartbeat loop: periodically pulls mail, thinks, acts
- Survives across tasks; not started/stopped per task
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..llm.mock import MockLLM
from ..monitor import Monitor
from ..models import (
    AuthorStatus,
    Decision,
    Mail,
    Persona,
    SessionContext,
    TickContext,
)
from ..policy import NetworkPolicy, RateLimiter
from ..storage.mailbox_db import MailboxDB
from ..storage.session_db import SessionDB
from .think import decide
from .routing import RECIPIENT_ALIASES, resolve_recipients as _resolve_recipients_impl


class Author:
    """一个长生命周期的 agent (作者)。

    Usage:
        persona = Persona(id="zhang", display_name="小张", ...)
        mailbox = MailboxDB("./data/mailbox.db")
        sessions = SessionDB("./data/sessions.db")
        llm = MockLLM()

        zhang = Author(persona, mailbox, sessions, llm)
        await zhang.start()  # 启动 heartbeat loop

        # 在外面给 zhang 发邮件
        await mailbox.deliver(Mail.new(sender="god", recipients=["zhang"], ...))

        # 30s 内 zhang 会 tick, 处理邮件, 发回复
    """

    def __init__(
        self,
        persona: Persona,
        mailbox: MailboxDB,
        sessions: SessionDB,
        llm: MockLLM,
        data_dir: str | Path | None = None,
        registry: "HeartbeatRegistry | None" = None,
        monitor: Monitor | None = None,
        rate_limiter: RateLimiter | None = None,
        policy: NetworkPolicy | None = None,
    ):
        self.persona = persona
        self.mailbox = mailbox
        self.sessions_db = sessions
        self.llm = llm
        self.data_dir = Path(data_dir) if data_dir else Path("./data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry  # 用来 burst 其他 author
        self.monitor = monitor  # 事件监控 (可选)
        self.rate_limiter = rate_limiter  # 流量控制 (可选)
        self.policy = policy or NetworkPolicy()  # 网络 policy

        # 跟踪 last tick 时间 (用于 cooldown)
        self._last_tick_at: datetime | None = None

        # 状态
        self.status: AuthorStatus = "idle"
        self.last_tick_at: datetime | None = None
        self.next_tick_at: datetime | None = None
        self.total_ticks: int = 0
        self.total_actions: int = 0

        # 活跃 sessions (cache, 由 _tick 维护)
        self.sessions: dict[str, SessionContext] = {}

        # 自己的活动 log (短期)
        self.activity_log: list[dict] = []

        # 控制
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._new_mail_event: asyncio.Event = asyncio.Event()  # burst trigger

    # ========================================================================
    # Lifecycle
    # ========================================================================

    async def start(self):
        """启动 author。加载状态 + 启动 heartbeat loop。"""
        if self._running:
            return
        # 加载已有 sessions
        await self._load_sessions()
        # 计算下次 tick
        self._schedule_next_tick()
        # 启动 loop
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        print(f"[{self.persona.id}] author started. next_tick={self.next_tick_at}")

    async def stop(self):
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        print(f"[{self.persona.id}] author stopped.")

    def trigger_immediate_tick(self):
        """外部触发 (新邮件来了), 不等到下一个 interval。"""
        self._new_mail_event.set()

    # ========================================================================
    # Tick & Heartbeat
    # ========================================================================

    def _interval_for(self) -> int:
        """根据工时返回心跳间隔 (秒)。"""
        if self.persona.is_on_duty:
            return self.persona.heartbeat_seconds
        else:
            return self.persona.off_duty_interval

    def _schedule_next_tick(self):
        interval = self._interval_for()
        self.next_tick_at = datetime.now() + timedelta(seconds=interval)

    async def _heartbeat_loop(self):
        """主循环: 等待 → tick → 等待 → tick → ..."""
        while self._running:
            # 等待: 直到 next_tick 或 burst event
            now = datetime.now()
            if self.next_tick_at and now < self.next_tick_at:
                wait_seconds = (self.next_tick_at - now).total_seconds()
                try:
                    await asyncio.wait_for(
                        self._new_mail_event.wait(), timeout=wait_seconds
                    )
                    self._new_mail_event.clear()
                    print(f"[{self.persona.id}] ⚡ burst tick")
                except asyncio.TimeoutError:
                    pass
            # Cooldown 检查 (强制间隔, 防模型卡顿重试)
            if self._last_tick_at:
                elapsed = (datetime.now() - self._last_tick_at).total_seconds()
                if elapsed < self.policy.min_tick_interval_seconds:
                    wait_more = self.policy.min_tick_interval_seconds - elapsed
                    print(f"[{self.persona.id}] ⏸ cooldown {wait_more:.1f}s")
                    await asyncio.sleep(wait_more)
            self._last_tick_at = datetime.now()
            # 跑一次 tick
            try:
                print(f"[{self.persona.id}] ▶ tick #{self.total_ticks + 1}")
                await self._tick()
                print(f"[{self.persona.id}] ✓ tick done, next in {self._interval_for()}s")
            except Exception as e:
                import traceback
                print(f"[{self.persona.id}] tick error: {e}")
                traceback.print_exc()
                self.status = "stalled"
            # 安排下次
            self._schedule_next_tick()

    async def _tick(self):
        """一次心跳: 拉邮件 → 处理 sessions → LLM 决策 → 行动。"""
        self.total_ticks += 1
        self.last_tick_at = datetime.now()
        self.status = "thinking"

        # 1. 拉新邮件
        new_mail = await self.mailbox.fetch_unread(
            owner=self.persona.id,
            since=datetime(1970, 1, 1),  # 全部未读
            limit=50,
        )

        # 2. 更新 sessions (新邮件进对应 session)
        for m in new_mail:
            await self._absorb_mail(m)

        # 3. 重新加载 active sessions
        await self._load_sessions()

        # 4. 构造 TickContext
        ctx = TickContext(
            persona=self.persona,
            new_mail=new_mail,
            active_sessions=list(self.sessions.values()),
            recent_own_activities=[a.get("summary", "") for a in self.activity_log[-20:]],
        )

        # 4.5 Monitor: 记录收邮件
        if self.monitor:
            for m in new_mail:
                self.monitor.mail_received(m, by_author=self.persona.id)

        # 5. LLM 决策
        if not new_mail and not self.sessions:
            self.status = "idle"
            return

        decision = await decide(ctx, self.llm)

        # 6. 执行决策
        await self._execute(decision, new_mail)

        # 7. 标记已读
        if new_mail:
            await self.mailbox.mark_read([m.id for m in new_mail])

        # 8. 更新 activity log
        self.activity_log.append({
            "ts": datetime.now().isoformat(),
            "summary": decision.thinking[:200],
            "status": decision.next_status,
            "n_new_mail": len(new_mail),
            "n_sessions": len(self.sessions),
        })
        # 只保留最近 100 条
        self.activity_log = self.activity_log[-100:]

        # 9. 写自己的 tick log 到磁盘
        await self._write_tick_log(decision, new_mail)

    async def _absorb_mail(self, m: Mail):
        """把邮件吸进对应的 session。"""
        sid = m.thread_id
        if sid not in self.sessions:
            self.sessions[sid] = SessionContext(
                thread_id=sid,
                topic=m.subject or "(无主题)",
                participants={m.sender, self.persona.id, *m.recipients},
            )
        s = self.sessions[sid]
        s.history_ids.append(m.id)
        s.last_activity = m.created_at
        # 如果是 reply,可能 closing
        if m.requires_ack:
            s.status = "active"
        # 持久化
        await self.sessions_db.upsert(self.persona.id, s)

    def _resolve_recipients(self, recipients) -> list[str]:
        """验证 + 重路由 recipients (委托给 routing 模块).

        策略:
        1. 已经是真实 author id → 保留
        2. alias_map 匹配 (dev, developer, team, 小张, etc) → 映射
        3. 模糊匹配 (子串/前缀) → 找最像的
        4. 找不到 → log warning + drop
        """
        return _resolve_recipients_impl(
            list(recipients),
            self.registry,
            persona_id=self.persona.id,
        )

    async def _execute(self, decision: Decision, new_mail: list[Mail]):
        """执行 LLM 决策。"""
        # 发邮件 (受 max_actions_per_tick + rate_limit 限制)
        actions_this_tick = 0
        for m in decision.outgoing_mail:
            if actions_this_tick >= self.policy.max_actions_per_tick:
                print(f"  [{self.persona.id}] ⚠ max_actions_per_tick 限额 ({self.policy.max_actions_per_tick}), 跳过剩余邮件")
                break
            # 强制 sender 是自己
            object.__setattr__(m, 'sender', self.persona.id)
            # 验证 + 重路由 recipients
            resolved_recipients = self._resolve_recipients(m.recipients)
            object.__setattr__(m, 'recipients', tuple(resolved_recipients))
            if not resolved_recipients:
                print(f"  [{self.persona.id}] ⚠ mail dropped, no valid recipients: {m.subject[:40]}")
                continue
            # Rate limit 检查
            if self.rate_limiter:
                ok, reason = self.rate_limiter.check(
                    self.persona.id,
                    self.policy.max_mails_per_hour,
                    self.policy.max_mails_per_day,
                )
                if not ok:
                    print(f"  [{self.persona.id}] ⚠ rate limited: {reason}, 跳过")
                    continue
            await self.mailbox.deliver(m)
            # 计数
            if self.rate_limiter:
                self.rate_limiter.increment(self.persona.id)
            # Monitor 记录
            if self.monitor:
                self.monitor.mail_sent(m, by_author=self.persona.id)
            # Burst 给收件人
            for r in resolved_recipients:
                if r != self.persona.id and self.registry:
                    other = self.registry.get(r)
                    if other:
                        other.trigger_immediate_tick()
            self.total_actions += 1
            actions_this_tick += 1
            print(f"  [{self.persona.id}] → mail to {resolved_recipients}: {m.subject[:40]}")

        # 关闭 sessions
        for sid in decision.closed_sessions:
            if sid in self.sessions:
                self.sessions[sid].status = "completed"
                await self.sessions_db.upsert(self.persona.id, self.sessions[sid])
                # Monitor 记录
                if self.monitor:
                    self.monitor.session_completed(self.persona.id, sid)
                # 不立刻删,保留记录
                print(f"  [{self.persona.id}] ✓ session completed: {sid}")

        # 工具调用 (MVP mock: 只记录)
        for action in decision.actions:
            if action.type == "use_tool":
                self.activity_log.append({
                    "ts": datetime.now().isoformat(),
                    "summary": f"tool call: {action.payload}",
                    "kind": "tool",
                })
                # Monitor 记录
                if self.monitor:
                    tool_name = action.payload.get("tool", "?") if isinstance(action.payload, dict) else "?"
                    tool_input = str(action.payload)[:200] if action.payload else ""
                    self.monitor.tool_used(self.persona.id, tool_name, tool_input)
                self.total_actions += 1
                print(f"  [{self.persona.id}] 🔧 tool: {action.payload}")

        # 状态
        self.status = decision.next_status

    # ========================================================================
    # Persistence
    # ========================================================================

    async def _load_sessions(self):
        """从 DB 加载所有 sessions 到内存。"""
        rows = await self.sessions_db.list_all(self.persona.id)
        self.sessions = {r.thread_id: r for r in rows}

    async def _write_tick_log(self, decision: Decision, new_mail: list[Mail]):
        """把每次 tick 写到磁盘 (for debug + Web UI)。"""
        log_path = self.data_dir / f"{self.persona.id}-ticks.jsonl"
        entry = {
            "ts": datetime.now().isoformat(),
            "tick": self.total_ticks,
            "status_before": "thinking",
            "status_after": self.status,
            "n_new_mail": len(new_mail),
            "n_active_sessions": len(self.sessions),
            "thinking": decision.thinking,
            "outgoing_mail_count": len(decision.outgoing_mail),
            "actions_count": len(decision.actions),
        }
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # ========================================================================
    # Observation (for Web UI)
    # ========================================================================

    def snapshot(self) -> dict[str, Any]:
        """供 Web UI 读取的状态快照。"""
        return {
            "persona": {
                "id": self.persona.id,
                "display_name": self.persona.display_name,
                "emoji": self.persona.emoji,
                "title": self.persona.title,
            },
            "llm_backend": self.persona.llm_backend,
            "llm_model": self.persona.llm_model,
            "status": self.status,
            "is_on_duty": self.persona.is_on_duty,
            "heartbeat_seconds": self._interval_for(),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "next_tick_at": self.next_tick_at.isoformat() if self.next_tick_at else None,
            "total_ticks": self.total_ticks,
            "total_actions": self.total_actions,
            "active_sessions": [
                {
                    "thread_id": s.thread_id,
                    "topic": s.topic,
                    "status": s.status,
                    "blocked_reason": s.blocked_reason,
                    "n_messages": len(s.history_ids),
                    "last_activity": s.last_activity.isoformat(),
                }
                for s in self.sessions.values()
                if s.status in ("active", "blocked", "stalled")
            ],
        }
