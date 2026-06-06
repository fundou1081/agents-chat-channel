"""
核心数据模型: Mail, Session, Author, Persona, Decision, Post, Channel
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal


# ============================================================================
# Mail (点对点消息, 已存在)
# ============================================================================


class MailPriority(int, Enum):
    LOW = 1
    NORMAL = 5
    HIGH = 9
    URGENT = 10


@dataclass(frozen=True)
class Mail:
    id: str
    sender: str
    recipients: tuple[str, ...]
    thread_id: str
    in_reply_to: str | None = None
    subject: str = ""
    body: str = ""
    attachments: tuple[dict, ...] = ()
    priority: int = MailPriority.NORMAL
    requires_ack: bool = False
    created_at: datetime = field(default_factory=datetime.now)
    metadata: dict = field(default_factory=dict)

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

    @classmethod
    def new(cls, sender, recipients, subject, body, thread_id=None, in_reply_to=None, **kwargs):
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


# ============================================================================
# Session
# ============================================================================


SessionStatus = Literal["active", "blocked", "completed", "stalled"]


@dataclass
class SessionContext:
    thread_id: str
    topic: str
    status: SessionStatus = "active"
    participants: set[str] = field(default_factory=set)
    history_ids: list[str] = field(default_factory=list)
    blocked_reason: str | None = None
    last_activity: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)
    summary: str = ""

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
# Post (新 - 合并 Bulletin + FreeChat)
# ============================================================================


PostKind = Literal["broadcast", "task", "discussion", "freechat"]
PostStatus = Literal["open", "claimed", "closed", "expired"]


@dataclass
class Post:
    """统一 Posts 抽象 (方案 B).

    kind:
      - "broadcast":  公告, 永久, role 匹配或 all
      - "task":       无主任务, 认领机制
      - "discussion": 讨论, mention 匹配
      - "freechat":   临时, max_rounds / expires_at / session_id
    """

    id: str
    kind: str                                # "broadcast" | "task" | "discussion" | "freechat"
    title: str = ""
    body: str = ""
    posted_by: str = "god"
    posted_at: str = ""
    tags: list[str] = field(default_factory=list)
    required_role: str = ""                   # task 才有
    claimed_by: str = ""                      # task 才有
    status: str = "open"                      # "open" | "claimed" | "closed" | "expired"
    expires_at: str = ""                      # freechat TTL
    max_rounds: int = 0                       # freechat 才有 (10)
    current_round: int = 0                    # freechat 计数
    session_id: str = ""                      # freechat 关联 session
    last_activity_at: str = ""                # freechat idle 判定

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Post":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ============================================================================
# Channel (新 - 持久主题频道)
# ============================================================================


@dataclass
class Channel:
    """持久公共频道 (Slack style)."""
    id: str
    name: str                                # "#frontend", "#random"
    description: str = ""
    created_by: str = "god"
    created_at: str = ""
    is_public: bool = True                    # True=自由加入, False=邀请制
    pinned_topic: str = ""                    # 置顶话题
    members: list[str] = field(default_factory=list)  # 缓存 (实际存 channel_members)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ChannelMessage:
    """频道里的一条消息."""
    id: str
    channel_id: str
    sender: str
    body: str
    posted_at: str
    reply_to: str | None = None               # thread 模式
    mentions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# Persona
# ============================================================================


@dataclass
class Persona:
    id: str
    display_name: str
    emoji: str = "🤖"
    title: str = ""
    system_prompt: str = ""
    workdir: str = "/tmp"
    heartbeat_seconds: int = 30
    sleep_hours: tuple[int, int] | None = (9, 22)
    off_duty_interval: int = 600
    llm_backend: str = "mock"
    llm_model: str | None = None

    @property
    def is_on_duty(self) -> bool:
        if self.sleep_hours is None:
            return True
        h = datetime.now().hour
        start, end = self.sleep_hours
        if start < end:
            return start <= h < end
        return h >= start or h < end


# ============================================================================
# Author Status
# ============================================================================


AuthorStatus = Literal[
    "idle", "thinking", "working", "blocked", "stalled", "off_duty",
]


# ============================================================================
# Decision
# ============================================================================


@dataclass
class Action:
    type: str
    payload: dict = field(default_factory=dict)


@dataclass
class Decision:
    thinking: str = ""
    actions: list[Action] = field(default_factory=list)
    outgoing_mail: list[Mail] = field(default_factory=list)
    closed_sessions: list[str] = field(default_factory=list)
    next_status: AuthorStatus = "idle"
    raw_response: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        actions = [Action(**a) for a in d.get("actions", [])]
        mail = []
        for m in d.get("outgoing_mail", []):
            if "sender" in m and isinstance(m.get("recipients"), list):
                mail.append(Mail.from_dict(m))
            else:
                # 兼容没有 sender 的 (sender 强制为 author)
                m2 = dict(m)
                m2["recipients"] = m2.get("recipients", [])
                m2.setdefault("thread_id", str(uuid.uuid4())[:8])
                m2.setdefault("in_reply_to", None)
                mail.append(Mail.from_dict(m2))
        return cls(
            thinking=d.get("thinking", ""),
            actions=actions,
            outgoing_mail=mail,
            closed_sessions=d.get("closed_sessions", []),
            next_status=d.get("next_status", "idle"),
            raw_response=d.get("raw_response", ""),
        )


# ============================================================================
# TickContext
# ============================================================================


@dataclass
class TickContext:
    persona: Persona
    new_mail: list[Mail] = field(default_factory=list)
    active_sessions: list[SessionContext] = field(default_factory=list)
    recent_own_activities: list[str] = field(default_factory=list)
    memory_recall: list[str] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)                    # 中央 Posts (pull)
    channel_messages: list[ChannelMessage] = field(default_factory=list)  # 订阅频道 (push)

    def to_prompt_dict(self) -> dict:
        return {
            "persona": {
                "id": self.persona.id,
                "display_name": self.persona.display_name,
                "title": self.persona.title,
            },
            "new_mail": [m.to_dict() for m in self.new_mail],
            "active_sessions": [s.to_dict() for s in self.active_sessions],
            "posts": [p.to_dict() for p in self.posts],
            "channel_messages": [c.to_dict() for c in self.channel_messages],
            "recent_activities": self.recent_own_activities[:10],
            "memory_recall": self.memory_recall,
        }


# ============================================================================
# DAG (Phase 3) — 并行调度
# ============================================================================


class DagStatus(str, Enum):
    """DAG 整体状态机.

    pending → active → (completed | failed | cancelled)
    """
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DagNodeStatus(str, Enum):
    """DAG 节点状态机.

    pending → running → (done | failed)
                    ↓
                  blocked (上游 failed)
    """
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass
class DagNode:
    """DAG 上的一个节点 (一个 author 要干的事).

    depends_on: 依赖的 node id 列表; 空 list = 无依赖, 可立即派发.
    """
    id: str                                  # DAG 内唯一 (e.g. "api", "ui", "integ")
    title: str = ""
    body: str = ""
    assignee: str = ""                       # 派给哪个 author (e.g. "li-backend")
    depends_on: list[str] = field(default_factory=list)
    status: str = "pending"                  # DagNodeStatus
    started_at: str = ""
    completed_at: str = ""
    dispatch_mail_id: str = ""               # 派发时发的 mail id
    report_mail_id: str = ""                 # author 回执的 mail id
    error: str = ""                          # 失败原因
    timeout_at: str = ""                     # 期望回执时间

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DagNode":
        return cls(**{k: v for k in v if k in cls.__dataclass_fields__})


@dataclass
class DAG:
    """一个完整任务的有向无环图.

    提交后由 OrchestratorAuthor 推进.
    """
    id: str
    title: str = ""
    description: str = ""
    created_by: str = "god"
    created_at: str = ""
    status: str = "pending"                  # DagStatus
    nodes: list[DagNode] = field(default_factory=list)
    post_id: str = ""                        # 关联的 dag_dispatch post
    completed_at: str = ""

    def get_node(self, node_id: str) -> DagNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    def ready_nodes(self) -> list[DagNode]:
        """返回所有 status=pending 且 deps 都 done 的节点 (可派发)."""
        done_ids = {n.id for n in self.nodes if n.status == "done"}
        return [
            n for n in self.nodes
            if n.status == "pending"
            and all(dep in done_ids for dep in n.depends_on)
        ]

    def is_terminal(self) -> bool:
        """DAG 是否到达终态."""
        return self.status in ("completed", "failed", "cancelled")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DAG":
        d = dict(d)
        nodes_data = d.pop("nodes", [])
        d["nodes"] = [DagNode.from_dict(n) for n in nodes_data]
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
