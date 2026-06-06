"""
Monitor: 监控 author 之间的对话事件, 写到 monitor.jsonl.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .models import Mail, Post, ChannelMessage


EXTERNAL_SENDERS = {"god", "user", "human", ""}


@dataclass
class Event:
    id: str
    timestamp: str
    kind: str
    actor: str
    thread_id: str | None = None
    mail_id: str | None = None
    mail_subject: str | None = None
    mail_from: str | None = None
    mail_to: list[str] = field(default_factory=list)
    session_topic: str | None = None
    tool_name: str | None = None
    tool_input: str | None = None
    summary: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Monitor:
    def __init__(self, log_path: str | Path):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, kind: str, actor: str, **kwargs) -> Event:
        with self._lock:
            ev = Event(
                id=str(uuid.uuid4())[:12], timestamp=datetime.now().isoformat(),
                kind=kind, actor=actor, **kwargs,
            )
            with open(self.log_path, "a") as f:
                f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")
            return ev

    def mail_sent(self, mail: Mail, by_author: str) -> Event:
        return self.record("mail_sent", actor=by_author,
            thread_id=mail.thread_id, mail_id=mail.id,
            mail_subject=mail.subject, mail_from=mail.sender, mail_to=list(mail.recipients),
            summary=f"→ {', '.join(mail.recipients)}: {mail.subject[:60]}")

    def mail_received(self, mail: Mail, by_author: str) -> Event:
        return self.record("mail_received", actor=by_author,
            thread_id=mail.thread_id, mail_id=mail.id,
            mail_subject=mail.subject, mail_from=mail.sender, mail_to=list(mail.recipients),
            summary=f"← {mail.sender}: {mail.subject[:60]}")

    def session_started(self, author: str, thread_id: str, topic: str) -> Event:
        return self.record("session_started", actor=author,
            thread_id=thread_id, session_topic=topic,
            summary=f"new session: {topic[:60]}")

    def session_completed(self, author: str, thread_id: str) -> Event:
        return self.record("session_completed", actor=author,
            thread_id=thread_id, summary=f"session done: {thread_id[:8]}")

    def tool_used(self, author: str, tool: str, input_summary: str = "") -> Event:
        return self.record("tool_used", actor=author,
            tool_name=tool, tool_input=input_summary[:200],
            summary=f"🔧 {tool}: {input_summary[:80]}")

    def post_claimed(self, author: str, post_id: str) -> Event:
        return self.record("post_claimed", actor=author,
            thread_id=post_id, summary=f"claimed: {post_id}")

    def channel_message(self, author: str, channel_id: str, body: str) -> Event:
        return self.record("channel_message", actor=author,
            thread_id=channel_id, summary=f"→ {channel_id}: {body[:50]}")

    def read_recent(self, limit: int = 200, only_agent: bool = True) -> list[dict]:
        if not self.log_path.exists():
            return []
        lines = self.log_path.read_text(errors="replace").strip().split("\n")
        events = []
        for line in reversed(lines[-limit:]):
            try:
                ev = json.loads(line)
                if only_agent and not self._is_agent_event(ev):
                    continue
                events.append(ev)
            except (json.JSONDecodeError, TypeError):
                pass
        return events

    def read_conversations(self, limit: int = 100) -> list[dict]:
        events = self.read_recent(limit=500, only_agent=True)
        sent = [e for e in events if e["kind"] in ("mail_sent", "channel_message")]
        return sent[:limit]

    def _is_agent_event(self, ev: dict) -> bool:
        actor = ev.get("actor", "")
        if actor in EXTERNAL_SENDERS:
            return False
        mail_to = ev.get("mail_to", [])
        if mail_to and all(r in EXTERNAL_SENDERS for r in mail_to):
            return False
        mail_from = ev.get("mail_from", "")
        if mail_from and mail_from in EXTERNAL_SENDERS:
            return False
        return True

    def stats(self) -> dict:
        all_events = self.read_recent(limit=10000, only_agent=False)
        agent_events = [e for e in all_events if self._is_agent_event(e)]
        by_kind: dict[str, int] = {}
        by_actor: dict[str, int] = {}
        for e in agent_events:
            by_kind[e["kind"]] = by_kind.get(e["kind"], 0) + 1
            by_actor[e["actor"]] = by_actor.get(e["actor"], 0) + 1
        return {
            "total_events": len(all_events),
            "agent_events": len(agent_events),
            "by_kind": by_kind,
            "by_actor": by_actor,
        }
