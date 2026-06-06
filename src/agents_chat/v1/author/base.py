"""
Author: long-lived agent with inbox + heartbeat + multi-session.

This is the core abstraction. An Author:
- Has a persistent identity (Persona)
- Has a Mailbox (SQLite-backed) for DM
- Has multiple in-flight Sessions
- Runs a Heartbeat loop: periodically pulls mail + posts + channels, thinks, acts
- Survives across tasks; not started/stopped per task

Pull-based info sources (author主动扫):
  - Mailbox (DM, 1-to-1)
  - Posts (公告/任务/讨论, 1-to-N, role/mention匹配)

Push-based info source (订阅推送):
  - Channels (持久频道, 订阅者自动收到新消息)
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
    ChannelMessage,
    Decision,
    Mail,
    Persona,
    SessionContext,
    TickContext,
)
from ..policy import NetworkPolicy, RateLimiter
from ..storage.channels_db import ChannelDB
from ..storage.mailbox_db import MailboxDB
from ..storage.posts_db import PostsDB
from ..storage.session_db import SessionDB
from .think import decide
from .routing import RECIPIENT_ALIASES, resolve_recipients as _resolve_recipients_impl


class Author:
    """一个长生命周期的 agent (作者).

    Usage:
        persona = Persona(id="zhang", display_name="小张", ...)
        mailbox = MailboxDB("./data/mailbox.db")
        posts = PostsDB("./data/posts.db")
        channels = ChannelDB("./data/channels.db")
        llm = MockLLM()

        zhang = Author(persona, mailbox, posts, channels, llm, ...)
        await zhang.start()
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
        posts: PostsDB | None = None,
        channels: ChannelDB | None = None,
    ):
        self.persona = persona
        self.mailbox = mailbox
        self.sessions_db = sessions
        self.llm = llm
        self.data_dir = Path(data_dir) if data_dir else Path("./data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.monitor = monitor
        self.rate_limiter = rate_limiter
        self.policy = policy or NetworkPolicy()
        self.posts = posts        # NEW: 统一 Posts (公告/任务/讨论/临时聊天)
        self.channels = channels  # NEW: 持久频道 (订阅推送)

        # 跟踪 last tick 时间 (用于 cooldown)
        self._last_tick_at: datetime | None = None
        # 跟踪 last channel poll (避免重复扫)
        self._last_channel_poll: datetime | None = None
        # 生命周期标志
        self._running = False
        self._heartbeat_task: asyncio.Task | None = None
        self._new_mail_event: asyncio.Event = asyncio.Event()

        # 状态
        self.status: AuthorStatus = "idle"
        self.last_tick_at: datetime | None = None
        self.next_tick_at: datetime | None = None
        self.total_ticks: int = 0
        self.total_actions: int = 0

        # 会话 (memory cache)
        self.sessions: dict[str, SessionContext] = {}

        # 活动 log
        self.activity_log: list[dict] = []

    # ========================================================================
    # Lifecycle
    # ========================================================================

    async def start(self):
        if self._running:
            return
        await self._load_sessions()
        self._schedule_next_tick()
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
        self._new_mail_event.set()

    # ========================================================================
    # Heartbeat
    # ========================================================================

    def _interval_for(self) -> int:
        if self.persona.is_on_duty:
            return self.persona.heartbeat_seconds
        return self.persona.off_duty_interval

    def _schedule_next_tick(self):
        interval = self._interval_for()
        self.next_tick_at = datetime.now() + timedelta(seconds=interval)

    async def _heartbeat_loop(self):
        while self._running:
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
            if self._last_tick_at:
                elapsed = (datetime.now() - self._last_tick_at).total_seconds()
                if elapsed < self.policy.min_tick_interval_seconds:
                    wait_more = self.policy.min_tick_interval_seconds - elapsed
                    print(f"[{self.persona.id}] ⏸ cooldown {wait_more:.1f}s")
                    await asyncio.sleep(wait_more)
            self._last_tick_at = datetime.now()
            try:
                print(f"[{self.persona.id}] ▶ tick #{self.total_ticks + 1}")
                await self._tick()
                print(f"[{self.persona.id}] ✓ tick done, next in {self._interval_for()}s")
            except Exception as e:
                import traceback
                print(f"[{self.persona.id}] tick error: {e}")
                traceback.print_exc()
                self.status = "stalled"
            self._schedule_next_tick()

    # ========================================================================
    # Tick
    # ========================================================================

    async def _tick(self):
        self.total_ticks += 1
        self.last_tick_at = datetime.now()
        self.status = "thinking"

        # 1. 拉 DM
        new_mail = await self.mailbox.fetch_unread(
            owner=self.persona.id, since=datetime(1970, 1, 1), limit=50,
        )

        # 2. 更新 sessions
        for m in new_mail:
            await self._absorb_mail(m)
        await self._load_sessions()

        # 3. 扫 Posts (pull-based)
        posts_for_me = []
        if self.posts:
            try:
                posts_for_me = await self.posts.list_for_author(self.persona, limit=10)
            except Exception as e:
                print(f"  [{self.persona.id}] ⚠ posts scan error: {e}")

        # 4. 扫订阅频道新消息 (push-based)
        channel_msgs = []
        if self.channels:
            try:
                # 默认扫最近 1 小时的新消息
                since = (self._last_channel_poll or
                         datetime.now() - timedelta(hours=1)).isoformat()
                channel_msgs = await self.channels.get_recent_for_authors(
                    [self.persona.id], since=since, limit=20,
                )
                self._last_channel_poll = datetime.now()
            except Exception as e:
                print(f"  [{self.persona.id}] ⚠ channels scan error: {e}")

        # 5. 构造 TickContext
        ctx = TickContext(
            persona=self.persona,
            new_mail=new_mail,
            active_sessions=list(self.sessions.values()),
            recent_own_activities=[a.get("summary", "") for a in self.activity_log[-20:]],
            posts=posts_for_me,
            channel_messages=channel_msgs,
        )

        # 6. Monitor: 记录收邮件
        if self.monitor:
            for m in new_mail:
                self.monitor.mail_received(m, by_author=self.persona.id)

        if not new_mail and not self.sessions and not posts_for_me and not channel_msgs:
            self.status = "idle"
            return

        # 7. LLM 决策
        decision = await decide(ctx, self.llm)

        # 8. 执行
        await self._execute(decision, new_mail, channel_msgs)

        # 9. 标记已读
        if new_mail:
            await self.mailbox.mark_read([m.id for m in new_mail])

        # 10. activity log
        self.activity_log.append({
            "ts": datetime.now().isoformat(),
            "summary": decision.thinking[:200],
            "status": decision.next_status,
            "n_new_mail": len(new_mail),
            "n_sessions": len(self.sessions),
            "n_posts": len(posts_for_me),
            "n_channel_msgs": len(channel_msgs),
        })
        self.activity_log = self.activity_log[-100:]

        # 11. tick log
        await self._write_tick_log(decision, new_mail)

    async def _absorb_mail(self, m: Mail):
        sid = m.thread_id
        if sid not in self.sessions:
            self.sessions[sid] = SessionContext(
                thread_id=sid, topic=m.subject or "(无主题)",
                participants={m.sender, self.persona.id, *m.recipients},
            )
        s = self.sessions[sid]
        s.history_ids.append(m.id)
        s.last_activity = m.created_at
        if m.requires_ack:
            s.status = "active"
        await self.sessions_db.upsert(self.persona.id, s)

    def _resolve_recipients(self, recipients) -> list[str]:
        return _resolve_recipients_impl(
            list(recipients), self.registry, persona_id=self.persona.id,
        )

    async def _execute(self, decision: Decision, new_mail: list[Mail], channel_msgs: list[ChannelMessage] = None):
        channel_msgs = channel_msgs or []
        actions_this_tick = 0

        # 1. 发邮件 (DM)
        for m in decision.outgoing_mail:
            if actions_this_tick >= self.policy.max_actions_per_tick:
                print(f"  [{self.persona.id}] ⚠ max_actions_per_tick, skip")
                break
            object.__setattr__(m, 'sender', self.persona.id)
            resolved = self._resolve_recipients(m.recipients)
            object.__setattr__(m, 'recipients', tuple(resolved))
            if not resolved:
                print(f"  [{self.persona.id}] ⚠ mail dropped, no valid recipients")
                continue
            if self.rate_limiter:
                ok, reason = self.rate_limiter.check(
                    self.persona.id,
                    self.policy.max_mails_per_hour,
                    self.policy.max_mails_per_day,
                )
                if not ok:
                    print(f"  [{self.persona.id}] ⚠ rate limited: {reason}")
                    continue
            await self.mailbox.deliver(m)
            if self.rate_limiter:
                self.rate_limiter.increment(self.persona.id)
            if self.monitor:
                self.monitor.mail_sent(m, by_author=self.persona.id)
            for r in resolved:
                if r != self.persona.id and self.registry:
                    other = self.registry.get(r)
                    if other:
                        other.trigger_immediate_tick()
            self.total_actions += 1
            actions_this_tick += 1
            print(f"  [{self.persona.id}] → mail to {resolved}: {m.subject[:40]}")

        # 2. 发频道消息
        ch_actions = [a for a in decision.actions if a.type == "post_channel_message"]
        for action in ch_actions:
            if actions_this_tick >= self.policy.max_actions_per_tick:
                break
            channel_id = action.payload.get("channel_id", "") if isinstance(action.payload, dict) else ""
            body = action.payload.get("body", "") if isinstance(action.payload, dict) else ""
            if not self.channels or not channel_id or not body:
                continue
            msg = self.channels.new_message(
                channel_id=channel_id, sender=self.persona.id, body=body,
            )
            await self.channels.post_message(msg)
            self.total_actions += 1
            actions_this_tick += 1
            if self.monitor:
                self.monitor.record("channel_message", actor=self.persona.id,
                                    thread_id=channel_id, summary=f"→ {channel_id}: {body[:50]}")
            # burst 频道订阅者
            members = await self.channels.list_members(channel_id)
            for m_id in members:
                if m_id != self.persona.id and self.registry:
                    other = self.registry.get(m_id)
                    if other:
                        other.trigger_immediate_tick()
            print(f"  [{self.persona.id}] → channel {channel_id}: {body[:40]}")

        # 3. 关 sessions
        for sid in decision.closed_sessions:
            if sid in self.sessions:
                self.sessions[sid].status = "completed"
                await self.sessions_db.upsert(self.persona.id, self.sessions[sid])
                if self.monitor:
                    self.monitor.session_completed(self.persona.id, sid)
                print(f"  [{self.persona.id}] ✓ session completed: {sid}")

        # 4. 其他 actions
        for action in decision.actions:
            if action.type == "use_tool":
                self.activity_log.append({
                    "ts": datetime.now().isoformat(),
                    "summary": f"tool call: {action.payload}",
                    "kind": "tool",
                })
                if self.monitor:
                    tool_name = action.payload.get("tool", "?") if isinstance(action.payload, dict) else "?"
                    self.monitor.tool_used(self.persona.id, tool_name, str(action.payload)[:200])
            elif action.type == "claim_post":
                # 认领 task 类 post
                post_id = action.payload.get("id", "") if isinstance(action.payload, dict) else ""
                if self.posts and post_id:
                    success, msg = await self.posts.claim(post_id, self.persona.id)
                    if success:
                        self.total_actions += 1
                        if self.monitor:
                            self.monitor.record("post_claimed", actor=self.persona.id,
                                                thread_id=post_id, summary=f"claimed: {post_id}")
                        print(f"  [{self.persona.id}] ✓ claimed post: {post_id}")
                    else:
                        print(f"  [{self.persona.id}] ✗ claim failed: {msg}")

        # 5. 状态
        self.status = decision.next_status

    # ========================================================================
    # Persistence
    # ========================================================================

    async def _load_sessions(self):
        rows = await self.sessions_db.list_all(self.persona.id)
        self.sessions = {r.thread_id: r for r in rows}

    async def _write_tick_log(self, decision, new_mail):
        log_path = self.data_dir / f"{self.persona.id}-ticks.jsonl"
        entry = {
            "ts": datetime.now().isoformat(),
            "tick": self.total_ticks,
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
    # Snapshot
    # ========================================================================

    def snapshot(self) -> dict[str, Any]:
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
