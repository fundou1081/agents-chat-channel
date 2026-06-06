"""
FastAPI web server: read-only view of authors + write endpoints for actions.

Endpoints:
  GET  /                  -> index.html
  GET  /api/authors       -> all author snapshots
  GET  /api/inbox/{id}    -> author's inbox
  GET  /api/sessions/{id} -> author's sessions
  GET  /api/mailbox       -> all mails
  GET  /api/ticks/{id}    -> tick log
  GET  /api/conversations -> agent↔agent events
  GET  /api/policy        -> policy + rate counts + free chat status
  POST /api/send          -> god sends a mail
  GET  /api/posts         -> list posts (公告/任务/讨论/临时)
  POST /api/posts/post    -> 发 post
  POST /api/posts/{id}/claim -> 认领 task
  POST /api/posts/{id}/close -> close
  POST /api/freechat      -> trigger free chat
  GET  /api/channels      -> list channels
  POST /api/channels/create -> 建频道
  POST /api/channels/{id}/join -> 加入
  POST /api/channels/{id}/leave -> 退出
  GET  /api/channels/{id}/messages -> 频道历史
  POST /api/channels/{id}/post -> 发频道消息
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..heartbeat import HeartbeatRegistry
from ..models import Mail
from ..monitor import Monitor
from ..storage.channels_db import ChannelDB
from ..storage.posts_db import PostsDB


UI_DIR = Path(__file__).parent / "ui"


class SendRequest(BaseModel):
    sender: str = "god"
    to: str
    subject: str
    body: str
    priority: int = 5
    requires_ack: bool = False


class PostRequest(BaseModel):
    kind: str = "broadcast"
    title: str = ""
    body: str = ""
    posted_by: str = "god"
    required_role: str = ""
    tags: list[str] = []
    expires_in_seconds: int = 0
    max_rounds: int = 0


class ClaimRequest(BaseModel):
    claimer: str = "god"


class ChannelCreateRequest(BaseModel):
    name: str
    description: str = ""
    created_by: str = "god"
    is_public: bool = True
    pinned_topic: str = ""


class ChannelJoinRequest(BaseModel):
    author_id: str = "god"


class ChannelMessageRequest(BaseModel):
    sender: str = "god"
    body: str
    reply_to: str | None = None


class FreeChatRequest(BaseModel):
    topic: str = "团队讨论"
    started_by: str = "god"


def create_app(registry: HeartbeatRegistry) -> FastAPI:
    app = FastAPI(title="agents-chat-channel")

    @app.on_event("startup")
    async def _init_storage():
        for a in registry.authors.values():
            await a.mailbox.fetch_unread("__init__", since=None, limit=1)
            await a.sessions_db.list_all("__init__")
        if registry.posts:
            await registry.posts.list_open(limit=1)
        if registry.channels:
            await registry.channels.list_channels()

    @app.get("/")
    async def index():
        index_path = UI_DIR / "index.html"
        if not index_path.exists():
            return JSONResponse({"error": "UI not found"}, status_code=404)
        return FileResponse(index_path)

    @app.get("/api/authors")
    async def get_authors() -> list[dict[str, Any]]:
        return registry.snapshots()

    @app.get("/api/inbox/{author_id}")
    async def get_inbox(author_id: str, limit: int = 50) -> list[dict]:
        author = registry.get(author_id)
        if not author:
            raise HTTPException(404, f"Author {author_id} not found")
        mails = await author.mailbox.fetch_inbox(owner=author_id, limit=limit)
        return [m.to_dict() for m in mails]

    @app.get("/api/sessions/{author_id}")
    async def get_sessions(author_id: str) -> list[dict]:
        author = registry.get(author_id)
        if not author:
            raise HTTPException(404, f"Author {author_id} not found")
        sessions = await author.sessions_db.list_all(author_id)
        return [s.to_dict() for s in sessions]

    @app.get("/api/mailbox")
    async def get_all_mailbox(limit: int = 100) -> list[dict]:
        import json as _json
        from ..main import get_data_dir
        import aiosqlite
        db_path = get_data_dir() / "mailbox.db"
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM mails ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"], "sender": r["sender"],
                "recipients": _json.loads(r["recipients"]),
                "thread_id": r["thread_id"], "in_reply_to": r["in_reply_to"],
                "subject": r["subject"] or "", "body": r["body"] or "",
                "priority": r["priority"], "requires_ack": bool(r["requires_ack"]),
                "created_at": r["created_at"], "read_at": r["read_at"],
            })
        return result

    @app.get("/api/ticks/{author_id}")
    async def get_ticks(author_id: str, limit: int = 50) -> list[dict]:
        from ..main import get_data_dir
        log_path = get_data_dir() / "logs" / f"{author_id}-ticks.jsonl"
        if not log_path.exists():
            return []
        lines = log_path.read_text().strip().split("\n")
        lines = lines[-limit:]
        import json as _json
        return [_json.loads(l) for l in lines if l.strip()]

    @app.get("/api/conversations")
    async def get_conversations(limit: int = 100) -> dict:
        from ..main import get_monitor
        monitor = get_monitor()
        events = monitor.read_conversations(limit=limit)
        stats = monitor.stats()
        return {"events": events, "stats": stats}

    @app.get("/api/policy")
    async def get_policy_info() -> dict:
        from ..main import get_rate_limiter, get_policy, get_posts_db
        policy = get_policy()
        rl = get_rate_limiter()
        counts = {}
        for a in registry.authors.values():
            pid = a.persona.id
            counts[pid] = {
                "hour": rl.get_count(pid, "hour"),
                "day": rl.get_count(pid, "day"),
                "max_per_hour": policy.max_mails_per_hour,
                "max_per_day": policy.max_mails_per_day,
            }
        # active free chats
        posts = get_posts_db()
        active_fc = []
        try:
            for p in await posts.list_active_freechats():
                active_fc.append({
                    "id": p.id, "topic": p.title, "round": p.current_round,
                    "max_rounds": p.max_rounds, "started_by": p.posted_by,
                })
        except Exception:
            pass
        return {
            "policy": policy.to_dict(),
            "counts": counts,
            "free_chats": active_fc,
        }

    @app.post("/api/send")
    async def send_mail(req: SendRequest) -> dict:
        author = registry.get(req.to)
        if not author:
            raise HTTPException(404, f"Author {req.to} not found")
        m = Mail.new(sender=req.sender, recipients=[req.to],
                    subject=req.subject, body=req.body,
                    priority=req.priority, requires_ack=req.requires_ack)
        await author.mailbox.deliver(m)
        author.trigger_immediate_tick()
        return {"ok": True, "mail_id": m.id, "thread_id": m.thread_id}

    @app.post("/api/freechat")
    async def trigger_freechat(req: FreeChatRequest) -> dict:
        post = registry.start_free_chat(req.topic, started_by=req.started_by)
        return {"ok": True, "post": post}

    # ----- Posts (公告/任务/讨论/临时聊天) -----

    @app.get("/api/posts")
    async def get_posts(kind: str | None = None, status: str = "open", limit: int = 50) -> dict:
        from ..main import get_posts_db
        db = get_posts_db()
        if status == "all":
            items = await db.list_all(limit=limit)
        else:
            items = await db.list_open(kind=kind, limit=limit)
        return {"items": [i.to_dict() for i in items], "count": len(items)}

    @app.post("/api/posts/post")
    async def post_post(req: PostRequest) -> dict:
        from ..main import get_posts_db
        db = get_posts_db()
        post = db.new(
            kind=req.kind, title=req.title, body=req.body,
            posted_by=req.posted_by, tags=req.tags,
            required_role=req.required_role,
            expires_in_seconds=req.expires_in_seconds,
            max_rounds=req.max_rounds,
        )
        await db.post(post)
        registry.trigger_burst_all()
        return post.to_dict()

    @app.post("/api/posts/{post_id}/claim")
    async def claim_post(post_id: str, req: ClaimRequest | None = None) -> dict:
        from ..main import get_posts_db
        db = get_posts_db()
        claimer = (req.claimer if req else "god") or "god"
        success, msg = await db.claim(post_id, claimer)
        if success and claimer in registry.authors:
            registry.authors[claimer].trigger_immediate_tick()
        return {"ok": success, "message": msg}

    @app.post("/api/posts/{post_id}/close")
    async def close_post(post_id: str) -> dict:
        from ..main import get_posts_db
        db = get_posts_db()
        ok = await db.close(post_id)
        return {"ok": ok}

    # ----- Channels (持久频道) -----

    @app.get("/api/channels")
    async def get_channels(author_id: str | None = None) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        if author_id:
            channels = await db.list_for_author(author_id)
        else:
            channels = await db.list_channels()
        # 加 members 数
        items = []
        for c in channels:
            d = c.to_dict()
            d["member_count"] = len(await db.list_members(c.id))
            items.append(d)
        return {"items": items, "count": len(items)}

    @app.post("/api/channels/create")
    async def create_channel(req: ChannelCreateRequest) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        try:
            ch = db.new_channel(
                name=req.name, description=req.description,
                created_by=req.created_by, is_public=req.is_public,
                pinned_topic=req.pinned_topic,
            )
            await db.create_channel(ch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return ch.to_dict()

    @app.post("/api/channels/{channel_id}/join")
    async def join_channel(channel_id: str, req: ChannelJoinRequest) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        ok = await db.join(channel_id, req.author_id)
        if ok and req.author_id in registry.authors:
            registry.authors[req.author_id].trigger_immediate_tick()
        return {"ok": ok}

    @app.post("/api/channels/{channel_id}/leave")
    async def leave_channel(channel_id: str, req: ChannelJoinRequest) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        ok = await db.leave(channel_id, req.author_id)
        return {"ok": ok}

    @app.get("/api/channels/{channel_id}/messages")
    async def get_channel_messages(channel_id: str, limit: int = 50) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        msgs = await db.list_messages(channel_id, limit=limit)
        return {"items": [m.to_dict() for m in msgs], "count": len(msgs)}

    @app.post("/api/channels/{channel_id}/post")
    async def post_channel_message(channel_id: str, req: ChannelMessageRequest) -> dict:
        from ..main import get_channels_db
        db = get_channels_db()
        msg = db.new_message(
            channel_id=channel_id, sender=req.sender, body=req.body, reply_to=req.reply_to,
        )
        await db.post_message(msg)
        # burst 频道订阅者
        members = await db.list_members(channel_id)
        for m_id in members:
            if m_id in registry.authors and m_id != req.sender:
                registry.authors[m_id].trigger_immediate_tick()
        return msg.to_dict()

    return app


async def start_web_server(registry: HeartbeatRegistry, port: int = 7331):
    import uvicorn
    app = create_app(registry)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
