"""
MockLLM: 规则-based 决策生成.

接口跟 QwenAgent / OpenCodeAgent 一样, 但不调真实 LLM.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from ..models import Action, Decision, Mail, Post, ChannelMessage, TickContext
from ..author.routing import RECIPIENT_ALIASES


class MockLLM:
    async def think(
        self, system: str, user: str, ctx: TickContext | None = None, tools: list[dict] | None = None,
    ) -> Decision:
        if ctx is None:
            return Decision(thinking="no ctx", next_status="idle")

        new_mail = ctx.new_mail
        posts = ctx.posts
        channel_msgs = ctx.channel_messages
        persona = ctx.persona

        outgoing: list[Mail] = []
        actions: list[Action] = []
        closed: list[str] = []
        thinking_parts: list[str] = []

        # 处理邮件
        for m in new_mail:
            subject = m.subject
            sender = m.sender
            body = m.body
            thread_id = m.thread_id

            if self._looks_like_task(body) or self._looks_like_task(subject):
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12], sender=persona.id,
                    recipients=(sender,), thread_id=thread_id, in_reply_to=m.id,
                    subject=f"Re: {subject}" if subject else "", body=body[:200],
                    priority=5, created_at=datetime.now(),
                ))
                actions.append(Action(type="use_tool", payload={"tool": "write", "input": f"task {subject}"}))
            elif m.requires_ack:
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12], sender=persona.id,
                    recipients=(sender,), thread_id=thread_id, in_reply_to=m.id,
                    subject=f"Re: {subject}" if subject else "",
                    body=f"已收到, ack 一下。\n— {persona.display_name}",
                    priority=5, created_at=datetime.now(),
                ))
                closed.append(thread_id)
            else:
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12], sender=persona.id,
                    recipients=(sender,), thread_id=thread_id, in_reply_to=m.id,
                    subject=f"Re: {subject}" if subject else "",
                    body=f"收到 ({sender}):\n\n{body[:200]}\n\n— {persona.display_name}",
                    priority=5, created_at=datetime.now(),
                ))

        # 处理 posts (公告/任务/讨论/临时聊天)
        for p in posts:
            if p.kind == "task" and not p.claimed_by and p.required_role in ("", "any") or \
               p.required_role.lower() in persona.title.lower():
                actions.append(Action(type="claim_post", payload={"id": p.id}))
                thinking_parts.append(f"claimed task {p.id}")
            elif p.kind == "broadcast":
                # 主动回个简单反应 (如果需要)
                pass

        # 处理 channel 消息 (简单 echo)
        ch_actions: list[Action] = []
        for m in channel_msgs:
            ch_actions.append(Action(type="post_channel_message", payload={
                "channel_id": m.channel_id,
                "body": f"Re: {m.body[:50]}",
            }))

        next_status = "working" if outgoing or actions or ch_actions else "idle"
        if posts and not outgoing and not actions:
            next_status = "blocked"

        return Decision(
            thinking=" | ".join(thinking_parts) or f"看了 {len(new_mail)} 邮件 + {len(posts)} posts + {len(channel_msgs)} 频道消息",
            actions=actions + ch_actions,
            outgoing_mail=outgoing,
            closed_sessions=closed,
            next_status=next_status,
        )

    def _looks_like_task(self, text: str) -> bool:
        if not text:
            return False
        task_keywords = ["请", "帮我", "麻烦", "需要", "做", "改", "修", "实现", "加", "删除",
                          "please", "do", "fix", "add", "remove", "implement", "[任务]"]
        text_lower = text.lower()
        return any(kw in text_lower for kw in task_keywords)
