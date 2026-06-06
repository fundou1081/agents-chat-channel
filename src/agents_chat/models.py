"""
核心数据模型: Mail, Session, Author, Persona, Decision

设计原则:
- Mail 是不可变消息 (frozen)
- Session 是 mutable state,但由 author 独占修改
- Author 是长生命周期对象,跨多个 session 和 tick
- Decision 是 tick 后的 LLM 决策输出
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


# ============================================================================
# Mail
# ============================================================================


class MailPriority(int, Enum):
    LOW = 1
    NORMAL = 5
    HIGH = 9
    URGENT = 10


@dataclass(frozen=True)
class Mail:
    """一封邮件 = 一条异步消息"""

    id: str
    sender: str                       # "god" | "pm" | "zhang-frontend"
    recipients: tuple[str, ...]       # 收件人列表
    thread_id: str                    # 同一 thread 共享 id
    in_reply_to: str | None = None    # 上一封邮件 id
    subject: str = ""
    body: str = ""
    attachments: tuple[dict, ...] = ()
    priority: int = MailPriority.NORMAL
    requires_ack: bool = False        # 是否需要 ack
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        sender: str,
        recipients: list[str],
        subject: str,
        body: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
        **kwargs,
    ) -> "Mail":
        return cls(
            id=str(uuid.uuid4())[:12],
            sender=sender,
            recipients=tuple(recipients),
            thread_id=thread_id or str(uuid.uuid4())[:8],
            in_reply_to=in_reply_to,
            subject=subject,
            body=body,
            **kwargs,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "thread_id": self.thread_id,
            "in_reply_to": self.in_reply_to,
            "subject": self.subject,
            "body": self.body,
            "attachments": list(self.attachments),
            "priority": self.priority,
            "requires_ack": self.requires_ack,
            "created_at": self.created_at.isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Mail":
        return cls(
            id=d["id"],
            sender=d["sender"],
            recipients=tuple(d["recipients"]),
            thread_id=d["thread_id"],
            in_reply_to=d.get("in_reply_to"),
            subject=d.get("subject", ""),
            body=d.get("body", ""),
            attachments=tuple(d.get("attachments", [])),
            priority=d.get("priority", MailPriority.NORMAL),
            requires_ack=d.get("requires_ack", False),
            created_at=datetime.fromisoformat(d["created_at"]) if "created_at" in d else datetime.now(),
            metadata=d.get("metadata", {}),
        )


# ============================================================================
# Session (author 内部的并行会话)
# ============================================================================


SessionStatus = Literal["active", "blocked", "completed", "stalled"]


@dataclass
class SessionContext:
    """一个 author 内部的一个会话 (类似人脑子里一个 thread)"""

    thread_id: str
    topic: str                        # 会话主题,LLM 可见
    status: SessionStatus = "active"
    participants: set[str] = field(default_factory=set)
    history_ids: list[str] = field(default_factory=list)  # mail ids,按时间序
    blocked_reason: str | None = None
    last_activity: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)
    summary: str = ""                 # 压缩的会话摘要

    def to_dict(self) -> dict:
        return {
            "thread_id": self.thread_id,
            "topic": self.topic,
            "status": self.status,
            "participants": sorted(self.participants),
            "history_ids": self.history_ids,
            "blocked_reason": self.blocked_reason,
            "last_activity": self.last_activity.isoformat(),
            "created_at": self.created_at.isoformat(),
            "summary": self.summary,
        }


# ============================================================================
# Author Status
# ============================================================================


AuthorStatus = Literal[
    "idle",        # 没事干,睡觉
    "thinking",    # 正在 LLM 调用
    "working",     # 在执行任务 (读文件 / 跑命令)
    "blocked",     # 等别人回复
    "stalled",     # 异常,等上帝/PM 介入
    "off_duty",    # 下班,低频 heartbeat
]


# ============================================================================
# Persona (author 的身份 + 配置)
# ============================================================================


@dataclass
class Persona:
    """一个 author 的身份和配置"""

    id: str                           # "zhang-frontend"
    display_name: str                 # "小张"
    emoji: str = "🤖"
    title: str = ""                   # "前端工程师"
    system_prompt: str = ""           # 给 LLM 的 system prompt
    workdir: str = "/tmp"
    heartbeat_seconds: int = 30       # 平时心跳间隔
    sleep_hours: tuple[int, int] | None = (9, 22)  # 工时 (开始, 结束), None = 24/7
    off_duty_interval: int = 600      # 下班时心跳间隔
    # LLM 后端配置
    llm_backend: str = "mock"         # "mock" | "qwen" | "opencode"
    llm_model: str | None = None      # 后端的具体 model (None 用默认)

    @property
    def is_on_duty(self) -> bool:
        if self.sleep_hours is None:
            return True
        h = datetime.now().hour
        start, end = self.sleep_hours
        if start < end:
            return start <= h < end
        else:  # 跨午夜 (e.g., 22-6)
            return h >= start or h < end


# ============================================================================
# Decision (tick 后 LLM 的输出)
# ============================================================================


@dataclass
class Action:
    """author 在 tick 中要执行的动作"""

    type: Literal["think", "use_tool", "send_mail", "wait", "complete_session"]
    payload: dict = field(default_factory=dict)


@dataclass
class Decision:
    """一次 LLM 调用的输出"""

    thinking: str = ""                # LLM 的思考
    actions: list[Action] = field(default_factory=list)
    outgoing_mail: list[Mail] = field(default_factory=list)
    closed_sessions: list[str] = field(default_factory=list)
    next_status: AuthorStatus = "idle"
    raw_response: str = ""            # debug 用

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        actions = [Action(**a) for a in d.get("actions", [])]
        mail = [Mail.from_dict(m) for m in d.get("outgoing_mail", [])]
        return cls(
            thinking=d.get("thinking", ""),
            actions=actions,
            outgoing_mail=mail,
            closed_sessions=d.get("closed_sessions", []),
            next_status=d.get("next_status", "idle"),
            raw_response=d.get("raw_response", ""),
        )


# ============================================================================
# TickContext (tick 时的状态快照, 给 LLM 看)
# ============================================================================


@dataclass
class TickContext:
    """一次 tick 时,author 看到的所有信息"""

    persona: Persona
    new_mail: list[Mail]                          # 新邮件
    active_sessions: list[SessionContext]         # 所有 active session
    recent_own_activities: list[str] = field(default_factory=list)  # 最近自己的动作
    memory_recall: list[str] = field(default_factory=list)          # 从 long-term 召回的

    def to_prompt_dict(self) -> dict:
        return {
            "persona": {
                "id": self.persona.id,
                "display_name": self.persona.display_name,
                "title": self.persona.title,
            },
            "new_mail": [m.to_dict() for m in self.new_mail],
            "active_sessions": [s.to_dict() for s in self.active_sessions],
            "recent_activities": self.recent_own_activities[:10],
            "memory_recall": self.memory_recall,
        }
