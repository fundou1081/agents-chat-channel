"""
Mock LLM. Echoes back sensible decisions based on simple rules.

Real LLMs (Claude Code, OpenCode) will replace this.
Interface:
  async def think(system: str, user: str, ctx: TickContext | None = None) -> Decision
  Returns a Decision object directly (not raw JSON string).
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from typing import Any

from ..models import Decision, Mail, TickContext


class MockLLM:
    """A rule-based mock that makes plausible decisions."""

    async def think(
        self,
        system: str,
        user: str,
        ctx: TickContext | None = None,
        tools: list[dict] | None = None,
    ) -> Decision:
        """Return a Decision object."""
        # 优先用 ctx (结构化), fallback 解析 user prompt
        if ctx is not None:
            new_mail_dicts = [
                {
                    "id": m.id, "sender": m.sender, "subject": m.subject,
                    "body": m.body, "thread_id": m.thread_id,
                    "priority": m.priority, "requires_ack": m.requires_ack,
                }
                for m in ctx.new_mail
            ]
            active_sessions = ctx.active_sessions
            persona = ctx.persona
        else:
            # fallback: 从 system 提取 persona
            persona_match = re.search(r"你是([^,，。\n]+)", system)
            persona_name = persona_match.group(1).strip() if persona_match else "agent"
            persona = None
            new_mail_dicts = self._extract_mail_from_user(user)
            active_sessions = []

        return self._decide(new_mail_dicts, active_sessions, persona)

    def _decide(
        self,
        new_mail: list[dict],
        active_sessions: list,
        persona: Any | None,
    ) -> Decision:
        persona_id = persona.id if persona else "agent"
        persona_name = persona.display_name if persona else "agent"

        outgoing: list[Mail] = []
        actions: list[dict] = []
        closed: list[str] = []
        thinking_parts: list[str] = []

        if not new_mail and not active_sessions:
            return Decision(
                thinking="没事干,继续 idle",
                next_status="idle",
            )

        for m in new_mail:
            subject = m.get("subject", "")
            sender = m.get("sender", "")
            body = m.get("body", "")
            thread_id = m.get("thread_id", "")

            # PM 特殊逻辑: 收到 god 的任务,拆给 zhang 和 li
            if persona_id == "pm" and sender == "god" and self._looks_like_task(body):
                thinking_parts.append(f"拆解任务并派活给团队")
                # 派给 zhang
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=("zhang-frontend",),
                    thread_id=str(uuid.uuid4())[:8],  # 派活是新 thread
                    in_reply_to=None,
                    subject=f"[子任务] 前端: 用户登录页",
                    body=f"请实现用户登录页 UI。\n\n详细需求:\n{body[:200]}",
                    priority=5,
                    created_at=datetime.now(),
                ))
                # 派给 li
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=("li-backend",),
                    thread_id=str(uuid.uuid4())[:8],  # 派活是新 thread
                    in_reply_to=None,
                    subject=f"[子任务] 后端: Auth API",
                    body=f"请实现 /api/auth (login/logout) 接口。\n\n详细需求:\n{body[:200]}",
                    priority=5,
                    created_at=datetime.now(),
                ))
                # 汇报给 god
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=(sender,),
                    thread_id=thread_id,
                    in_reply_to=m.get("id"),
                    subject=f"Re: {subject}" if subject else "Re: 任务",
                    body=f"已拆解,派给前端 (小张) + 后端 (小李),预计并行完成。",
                    priority=5,
                    created_at=datetime.now(),
                ))
                continue

            if self._looks_like_task(body) or self._looks_like_task(subject):
                # 任务 → 模拟执行
                thinking_parts.append(f"收到 {sender} 的任务: {subject}")
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=(sender,),
                    thread_id=thread_id,
                    in_reply_to=m.get("id"),
                    subject=f"Re: {subject}" if subject else "",
                    body=f"收到任务。我开始处理:\n\n{body[:200]}\n\n[{persona_name} 在思考中...]",
                    priority=5,
                    created_at=datetime.now(),
                ))
                actions.append({
                    "type": "use_tool",
                    "payload": {"tool": "think", "input": f"分析任务: {subject}"},
                })
            elif m.get("requires_ack"):
                thinking_parts.append(f"ack {sender}")
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=(sender,),
                    thread_id=thread_id,
                    in_reply_to=m.get("id"),
                    subject=f"Re: {subject}" if subject else "",
                    body=f"已收到, ack 一下。\n— {persona_name}",
                    priority=5,
                    created_at=datetime.now(),
                ))
                # 关闭这个 session
                closed.append(thread_id)
            else:
                thinking_parts.append(f"回复 {sender}")
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=persona_id,
                    recipients=(sender,),
                    thread_id=thread_id,
                    in_reply_to=m.get("id"),
                    subject=f"Re: {subject}" if subject else "",
                    body=f"收到 ({sender}):\n\n{body[:200]}\n\n— {persona_name}",
                    priority=5,
                    created_at=datetime.now(),
                ))

        # 状态决定
        if actions:
            next_status = "working"
        elif outgoing and any(self._looks_like_task(m.get("body", "")) or self._looks_like_task(m.get("subject", "")) for m in new_mail):
            next_status = "working"
        elif active_sessions:
            next_status = "blocked"
        else:
            next_status = "idle"

        # 防死循环: 如果一个 thread 已经 5 轮以上了, 主动关掉
        thread_reply_count: dict[str, int] = {}
        for s in active_sessions:
            tid = getattr(s, 'thread_id', None) or s.get('thread_id', '')
            n = len(getattr(s, 'history_ids', []) or s.get('history_ids', []))
            thread_reply_count[tid] = n

        # 过沣 outgoing: 如果某封发出去的 thread 已经 5+ 轮, 不发, 反而关闭
        filtered_outgoing = []
        for m in outgoing:
            n = thread_reply_count.get(m.thread_id, 0)
            if n >= 5:
                # 主动关掉这个 session
                if m.thread_id not in closed:
                    closed.append(m.thread_id)
            else:
                filtered_outgoing.append(m)
        outgoing = filtered_outgoing

        if closed and not outgoing:
            thinking_parts.append(f"关闭 {len(closed)} 个完成的 session")

        return Decision(
            thinking=" | ".join(thinking_parts) or f"看到 {len(new_mail)} 邮件",
            actions=[self._mk_action(a) for a in actions],
            outgoing_mail=outgoing,
            closed_sessions=closed,
            next_status=next_status,
        )

    def _mk_action(self, a_dict: dict):
        from ..models import Action
        return Action(**a_dict)

    def _extract_mail_from_user(self, user: str) -> list[dict]:
        """从 user prompt 中 regex 抽取 new_mail (作为 fallback)."""
        # 简单版: 不解析,返回空
        return []

    def _looks_like_task(self, text: str) -> bool:
        if not text:
            return False
        task_keywords = ["请", "帮我", "麻烦", "需要", "做", "改", "修", "实现", "加", "删除",
                          "please", "do", "fix", "add", "remove", "implement", "[任务]"]
        text_lower = text.lower()
        return any(kw in text_lower for kw in task_keywords)
