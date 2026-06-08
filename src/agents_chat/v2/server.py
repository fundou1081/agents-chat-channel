"""
FastAPI Server for v2.0 — HTTP API + WebUI 静态文件.

端点 (REST):
  GET  /api/health                  health check
  GET  /api/channels                列出所有频道
  GET  /api/channels/{name}/messages 频道消息
  POST /api/channels/{name}/messages 发消息
  DELETE /api/channels/{name}/messages 清空频道消息
  GET  /api/channels/{name}/meta     频道成员/admins
  POST /api/channels/{name}/members  加成员
  POST /api/channels/{name}/admins   加 admin

  GET  /api/agents                  列出所有 worker (mailbox 文件)
  GET  /api/agents/{id}             worker 详情 (snapshot)
  POST /api/agents/{id}/start       启动 worker
  POST /api/agents/{id}/stop        停止 worker
  GET  /api/agents/{id}/log         worker 日志 (tail)

  GET  /api/mailboxes/{id}          agent 邮箱
  DELETE /api/mailboxes/{id}        清空邮箱

  GET  /api/sessions/{id}           worker sessions
  GET  /api/sessions/{id}/active    worker active sessions

  GET  /api/state_board             全局状态板
  GET  /api/state_board/{task_id}   某个 task 状态

  POST /api/reset                   重置 (停 worker + 清 sessions/mailboxes)

  GET  /api/stats 简单统计

  静态文件:
  GET  /webui/*                     WebUI (./webui/)

启动:
  python -m agents_chat.v2.server --port 8765 --data-dir ./data_v2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .files.channel import Channel
from .files.mailbox import Mailbox
from .session_manager import SessionManager
from .state_board import StateBoard


# =============================================================================
# 数据模型
# =============================================================================


class PostMessageRequest(BaseModel):
    from_: str = Field(alias="from")
    content: str
    type: str = "text"
    mentions: list[str] = []
    ref_msg_id: str = ""
    task_id: str = ""


class AddMemberRequest(BaseModel):
    agent_id: str


class AddAdminRequest(BaseModel):
    agent_id: str
    is_human: bool = False


# =============================================================================
# App Factory
# =============================================================================


def create_app(data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> FastAPI:
    """构造 FastAPI app."""

    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["channels", "mailboxes", "sessions", "locks"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        print(f"[server] ▶ http://{host}:{port}  data_dir={data_dir}")
        yield
        print("[server] ⏹ stopped")

    app = FastAPI(
        title="agents-chat-channel v2.0 Server",
        version="2.0.0",
        lifespan=lifespan,
    )

    # CORS: 全开 (WebUI 之后接)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------------------------------
    # Health
    # -------------------------------------------------------------------------

    @app.get("/api/health")
    def health():
        return {"ok": True, "ts": time.time()}

    # -------------------------------------------------------------------------
    # Channels
    # -------------------------------------------------------------------------

    @app.get("/api/channels")
    def list_channels():
        channels = []
        for ch_path in sorted((data_dir / "channels").glob("*.jsonl")):
            name = ch_path.stem  # "general" from "general.jsonl"
            ch = Channel(ch_path, name)
            msgs = ch.read(tail=1)
            channels.append({
                "name": name,
                "messages": ch.count(),
                "members": ch.list_members(),
                "admins": ch.list_admins(),
                "human_admins": ch.list_human_admins(),
            })
        return {"channels": channels, "count": len(channels)}

    @app.get("/api/channels/{name}/messages")
    def get_channel_messages(
        name: str,
        limit: int = Query(100, ge=1, le=1000),
        before: int = Query(0, ge=0),
    ):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        messages = ch.read(limit=limit, before=before)
        return {"messages": messages, "count": len(messages)}

    @app.post("/api/channels/{name}/messages")
    def post_message(name: str, req: PostMessageRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        ch = Channel(ch_path, name)
        msg_id = ch.append(
            from_=req.from_,
            content=req.content,
            type=req.type,
            mentions=req.mentions,
            ref_msg_id=req.ref_msg_id,
            task_id=req.task_id,
        )
        return {"ok": True, "msg_id": msg_id}

    @app.delete("/api/channels/{name}/messages")
    def clear_channel_messages(name: str):
        """清空频道所有消息 (保留频道本身)."""
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch_path.write_text("", encoding="utf-8")
        return {"ok": True, "cleared": name}

    @app.get("/api/channels/{name}/meta")
    def channel_meta(name: str):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        return {
            "name": name,
            "members": ch.list_members(),
            "admins": ch.list_admins(),
            "human_admins": ch.list_human_admins(),
        }

    @app.post("/api/channels/{name}/members")
    def add_member(name: str, req: AddMemberRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        ch.add_member(req.agent_id)
        return {"ok": True}

    @app.post("/api/channels/{name}/admins")
    def add_admin(name: str, req: AddAdminRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        ch.add_admin(req.agent_id, is_worker=not req.is_human)
        return {"ok": True}

    # -------------------------------------------------------------------------
    # Agents / Workers
    # -------------------------------------------------------------------------

    @app.get("/api/agents")
    def list_agents():
        """列出所有 worker (按 mailbox 文件)."""
        agents = []
        for mb_path in sorted((data_dir / "mailboxes").glob("*.json")):
            agent_id = mb_path.stem
            mb = Mailbox(mb_path, agent_id)
            pending = mb.peek()
            agents.append({
                "agent_id": agent_id,
                "pending": len(pending),
                "log_path": str(data_dir / "logs" / f"{agent_id}.log"),
            })
        return {"agents": agents, "count": len(agents)}

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str):
        """agent 快照: sessions + mailbox + state_board."""
        sb = StateBoard(data_dir / "state_board.json")
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        mb = Mailbox(mb_path, agent_id)
        sessions_dir = data_dir / "sessions"
        sm = SessionManager(sessions_dir / f"{agent_id}.json", agent_id)
        return {
            "agent_id": agent_id,
            "pending_mails": mb.peek(),
            "active_sessions": [s.to_dict() for s in sm.list_active()],
            "all_sessions": [s.to_dict() for s in sm.list_all()],
            "tasks": sb.list_by_agent(agent_id),
        }

    @app.post("/api/agents/{agent_id}/start")
    def start_agent(agent_id: str):
        """启动 worker 进程 (后台异步执行,立即返回)."""
        import subprocess, sys
        log_path = data_dir / "logs" / f"{agent_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(
            [sys.executable, "-m", "agents_chat.v2.main",
             "run-worker", agent_id, "--data-dir", str(data_dir)],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
        )
        return {"ok": True, "process": {"agent_id": agent_id, "pid": p.pid}}

    @app.post("/api/agents/{agent_id}/stop")
    def stop_agent(agent_id: str):
        """通过发 stop 邮件停止 worker."""
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        mb = Mailbox(mb_path, agent_id)
        mb.push(to=agent_id, subject="__STOP__", body="")
        return {"ok": True}

    @app.get("/api/agents/{agent_id}/log")
    def get_agent_log(agent_id: str, tail: int = Query(50, ge=1, le=500)):
        log_path = data_dir / "logs" / f"{agent_id}.log"
        if not log_path.exists():
            return {"log": "", "lines": 0}
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"log": "\n".join(lines[-tail:]), "lines": len(lines)}

    # -------------------------------------------------------------------------
    # Mailboxes
    # -------------------------------------------------------------------------

    @app.get("/api/mailboxes/{agent_id}")
    def get_mailbox(agent_id: str):
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        mb = Mailbox(mb_path, agent_id)
        return {"mails": mb.peek(), "count": mb.count()}

    @app.delete("/api/mailboxes/{agent_id}")
    def clear_mailbox(agent_id: str):
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if mb_path.exists():
            mb_path.write_text(
                json.dumps({"agent": agent_id, "pending": []}, ensure_ascii=False),
                encoding="utf-8"
            )
        return {"ok": True}

    # -------------------------------------------------------------------------
    # Sessions
    # -------------------------------------------------------------------------

    @app.get("/api/sessions/{agent_id}")
    def get_sessions(agent_id: str):
        sessions_dir = data_dir / "sessions"
        sm = SessionManager(sessions_dir / f"{agent_id}.json", agent_id)
        return {"sessions": [s.to_dict() for s in sm.list_all()], "count": len(sm.list_all())}

    @app.get("/api/sessions/{agent_id}/active")
    def get_active_sessions(agent_id: str):
        sessions_dir = data_dir / "sessions"
        sm = SessionManager(sessions_dir / f"{agent_id}.json", agent_id)
        return {"sessions": [s.to_dict() for s in sm.list_active()], "count": len(sm.list_active())}

    # -------------------------------------------------------------------------
    # State Board
    # -------------------------------------------------------------------------

    @app.get("/api/state_board")
    def get_state_board():
        sb = StateBoard(data_dir / "state_board.json")
        data = sb._read_unlocked()
        return {"tasks": data.get("tasks", {})}  # 始终返回 {"tasks": {...}}

    @app.get("/api/state_board/{task_id}")
    def get_task(task_id: str):
        sb = StateBoard(data_dir / "state_board.json")
        task = sb.get(task_id)
        if not task:
            raise HTTPException(404, f"task {task_id} not found")
        return {"task": task}

    # -------------------------------------------------------------------------
    # Reset
    # -------------------------------------------------------------------------

    @app.post("/api/reset")
    def reset_all():
        """重置: 停所有 worker + 清空 sessions + 清空 mailboxes (保留 channels)."""
        results = {
            "sessions_cleared": 0,
            "mailboxes_cleared": 0,
        }

        # 清空 sessions
        sessions_dir = data_dir / "sessions"
        if sessions_dir.exists():
            for f in sessions_dir.glob("*.json"):
                f.write_text(
                    json.dumps({"agent": f.stem, "sessions": {}}, ensure_ascii=False),
                    encoding="utf-8"
                )
                results["sessions_cleared"] += 1

        # 清空 mailboxes
        mailboxes_dir = data_dir / "mailboxes"
        if mailboxes_dir.exists():
            for f in mailboxes_dir.glob("*.json"):
                f.write_text(
                    json.dumps({"agent": f.stem, "pending": []}, ensure_ascii=False),
                    encoding="utf-8"
                )
                results["mailboxes_cleared"] += 1

        return {"ok": True, **results}

    # -------------------------------------------------------------------------
    # Stats
    # -------------------------------------------------------------------------

    @app.get("/api/stats")
    def get_stats():
        channels = len(list((data_dir / "channels").glob("*.jsonl")))
        agents = len(list((data_dir / "mailboxes").glob("*.json")))
        sb = StateBoard(data_dir / "state_board.json")
        tasks = len(sb.list_all())
        return {"channels": channels, "agents": agents, "tasks": tasks}

    # -------------------------------------------------------------------------
    # WebUI Static
    # -------------------------------------------------------------------------

    webui_dir = Path(__file__).parent.parent.parent / "webui"
    if webui_dir.exists():
        app.mount("/webui", StaticFiles(directory=str(webui_dir), html=True), name="webui")

    return app


# =============================================================================
# CLI 入口
# =============================================================================


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="agents-chat-channel v2.0 Server")
    parser.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    app = create_app(data_dir=data_dir, host=args.host, port=args.port)

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. pip install 'uvicorn[standard]>=0.27'")
        sys.exit(1)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()


__all__ = ["create_app", "main"]