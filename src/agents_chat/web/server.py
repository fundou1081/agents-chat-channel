"""
FastAPI web server: read-only view of authors + their inboxes.

Endpoints:
- GET  /              -> index.html
- GET  /api/authors   -> all author snapshots
- GET  /api/inbox/{id} -> author's inbox
- GET  /api/sessions/{id} -> author's sessions
- GET  /api/mailbox   -> all mails in DB
- POST /api/send      -> god sends a mail
- GET  /api/ticks/{id} -> recent tick log
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


UI_DIR = Path(__file__).parent / "ui"


class SendRequest(BaseModel):
    sender: str = "god"
    to: str
    subject: str
    body: str
    priority: int = 5
    requires_ack: bool = False


def create_app(registry: HeartbeatRegistry) -> FastAPI:
    app = FastAPI(title="agents-chat-channel")

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
        # 直接 query DB
        import json as _json
        import aiosqlite
        from ..main import get_data_dir
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
                "id": r["id"],
                "sender": r["sender"],
                "recipients": _json.loads(r["recipients"]),
                "thread_id": r["thread_id"],
                "in_reply_to": r["in_reply_to"],
                "subject": r["subject"] or "",
                "body": r["body"] or "",
                "priority": r["priority"],
                "requires_ack": bool(r["requires_ack"]),
                "created_at": r["created_at"],
                "read_at": r["read_at"],
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

    @app.post("/api/send")
    async def send_mail(req: SendRequest) -> dict:
        author = registry.get(req.to)
        if not author:
            raise HTTPException(404, f"Author {req.to} not found")

        from ..models import MailPriority
        m = Mail.new(
            sender=req.sender,
            recipients=[req.to],
            subject=req.subject,
            body=req.body,
            priority=req.priority,
            requires_ack=req.requires_ack,
        )
        await author.mailbox.deliver(m)
        # Burst trigger
        registry.trigger_burst(req.to)
        return {"ok": True, "mail_id": m.id, "thread_id": m.thread_id}

    return app


async def start_web_server(registry: HeartbeatRegistry, port: int = 7331):
    """Start uvicorn with our app."""
    import uvicorn
    app = create_app(registry)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()
