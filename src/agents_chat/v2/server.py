"""
FastAPI Server for v2.0 — HTTP API + WebUI 静态文件.

端点 (REST):
  GET  /                              health check
  GET  /api/agents                    列出所有 agent (mailbox 文件)
  GET  /api/agents/{id}               agent 详情 (snapshot)
  POST /api/agents/{id}/start         启动 agent 进程
  POST /api/agents/{id}/stop          停止 agent 进程
  GET  /api/agents/{id}/process       agent 进程状态
  GET  /api/agents/{id}/log           agent 日志 (tail)
  POST /api/agents/{id}/tick          触发 agent 立即 tick (mailbox 唤醒)

  GET  /api/channels                  列出所有频道
  GET  /api/channels/{name}/messages  频道消息 (tail)
  POST /api/channels/{name}/messages  发消息到频道 (mention/task_broadcast)
  GET  /api/channels/{name}/meta      频道元数据 (members + admins)
  POST /api/channels/{name}/members   加成员
  POST /api/channels/{name}/admins    加 admin (worker or human)

  GET  /api/mailboxes/{id}            agent 邮箱 pending 邮件
  DELETE /api/mailboxes/{id}          清空邮箱

  GET  /api/sessions/{id}             agent 全部 sessions
  GET  /api/sessions/{id}/active      agent active sessions
  POST /api/sessions/{id}/decide      decide_session LLM-free API
                                       (测试 SessionManager.decide_session)

  GET  /api/state_board               全局状态板
  GET  /api/state_board/{task_id}     某个 task 状态
  GET  /api/scanner/status            scanner offset
  GET  /api/stats                     简单统计

  POST /api/scanner/start             启动 scanner
  POST /api/scanner/stop              停止 scanner
  POST /api/scheduler/start           启动 scheduler
  POST /api/scheduler/stop            停止 scheduler

  GET  /api/processes                 列出所有 managed 进程
  GET  /api/processes/{id}            进程详情
  POST /api/processes/{id}/stop       停止进程

 静态文件 (之后接 WebUI):
  GET  /webui/*                       静态文件 (./webui/ 目录)

启动:
  python -m agents_chat.v2.server --port 8765 --data-dir ./data_v2
"""
from __future__ import annotations

import argparse
import asyncio
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
from .process_manager import ManagedProcess, ProcessManager
from .session_manager import SessionManager
from .state_board import StateBoard


# =============================================================================
# Pydantic models
# =============================================================================


class StartAgentRequest(BaseModel):
    cli: str = "mock"
    capabilities: list[str] = Field(default_factory=list)
    channel: str = "general"
    system_prompt: str = ""
    workspace_dir: str | None = None
    poll_interval: float = 2.0


class PostMessageRequest(BaseModel):
    content: str
    from_: str = Field(default="god", alias="from")
    type: str = "mention"
    mentions: list[str] = Field(default_factory=list)
    ref_msg_id: str = ""
    task_id: str = ""

    model_config = {"populate_by_name": True}


class AddMemberRequest(BaseModel):
    agent_id: str


class AddAdminRequest(BaseModel):
    agent_id: str
    is_worker: bool = True


class DecideRequest(BaseModel):
    task_id: str
    topic: str
    channel: str = ""


# =============================================================================
# App factory
# =============================================================================


def create_app(data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> FastAPI:
    """构造 FastAPI app."""

    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["channels", "mailboxes", "sessions", "locks"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    pm = ProcessManager(data_dir=data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup: 清理已退出的进程
        pm.cleanup_finished()
        print(f"[server] ▶ http://{host}:{port}  data_dir={data_dir}")
        yield
        # Shutdown: 停所有进程
        pm.stop_all()
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

    # ====================== Health ======================

    @app.get("/")
    def root():
        return {
            "ok": True,
            "name": "agents-chat-channel v2.0",
            "data_dir": str(data_dir),
            "endpoints": "/docs",
        }

    @app.get("/api/health")
    def health():
        return {"ok": True, "ts": time.time()}

    # ====================== Agents ======================

    @app.get("/api/agents")
    def list_agents():
        """列所有 agent (从 mailboxes/ 目录扫)."""
        mb_dir = data_dir / "mailboxes"
        agents = []
        for p in sorted(mb_dir.glob("*.json")):
            agent_id = p.stem
            mb = Mailbox(p, agent_id)
            proc = pm.get_agent_process(agent_id)
            agents.append({
                "agent_id": agent_id,
                "mailbox_count": mb.count(),
                "running": proc.is_running() if proc else False,
                "pid": proc.pid if proc else 0,
                "process_id": proc.process_id if proc else "",
            })
        return {"agents": agents, "count": len(agents)}

    @app.get("/api/agents/{agent_id}")
    def get_agent(agent_id: str):
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if not mb_path.exists():
            raise HTTPException(404, f"agent {agent_id} not found")
        mb = Mailbox(mb_path, agent_id)
        sess_path = data_dir / "sessions" / f"{agent_id}.json"
        sess = SessionManager(sess_path, agent_id) if sess_path.exists() else None
        proc = pm.get_agent_process(agent_id)
        return {
            "agent_id": agent_id,
            "mailbox_count": mb.count(),
            "active_sessions": len(sess.list_active()) if sess else 0,
            "total_sessions": len(sess.list_all()) if sess else 0,
            "process": proc.to_dict() if proc else None,
            "workspace_dir": str(data_dir / "workspaces" / agent_id),
        }

    @app.post("/api/agents/{agent_id}/start")
    def start_agent(agent_id: str, req: StartAgentRequest):
        try:
            proc = pm.start_agent(
                agent_id=agent_id,
                cli=req.cli,
                capabilities=req.capabilities,
                channel=req.channel,
                system_prompt=req.system_prompt,
                workspace_dir=req.workspace_dir,
                poll_interval=req.poll_interval,
            )
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "process": proc.to_dict()}

    @app.post("/api/agents/{agent_id}/stop")
    def stop_agent(agent_id: str):
        if not pm.stop_by_agent_id(agent_id):
            raise HTTPException(404, f"agent {agent_id} not running")
        return {"ok": True}

    @app.post("/api/agents/{agent_id}/tick")
    def tick_agent(agent_id: str):
        """触发 agent 立即 tick (写一个特殊 mail 让它立刻处理)."""
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if not mb_path.exists():
            raise HTTPException(404, f"agent {agent_id} not found")
        mb = Mailbox(mb_path, agent_id)
        mb.append(
            type="system_notify",
            content="[server] tick requested",
            channel="",
            ref_msg_id="",
            extra={"source": "server", "action": "tick"},
        )
        return {"ok": True, "tick_sent": True}

    @app.get("/api/agents/{agent_id}/log")
    def agent_log(agent_id: str, tail: int = Query(100, ge=1, le=1000)):
        proc = pm.get_agent_process(agent_id)
        if not proc:
            raise HTTPException(404, f"agent {agent_id} process not found")
        log = pm.read_log(proc.process_id, tail=tail)
        return {"agent_id": agent_id, "process_id": proc.process_id, "log": log}

    # ====================== Channels ======================

    @app.get("/api/channels")
    def list_channels():
        ch_dir = data_dir / "channels"
        chs = []
        for p in sorted(ch_dir.glob("*.jsonl")):
            name = p.stem
            ch = Channel(p, name)
            chs.append({
                "name": name,
                "messages": len(ch),
                "members": ch.list_members(),
                "admins": ch.list_admins(),
                "human_admins": ch.list_human_admins(),
            })
        return {"channels": chs, "count": len(chs)}

    @app.get("/api/channels/{name}/messages")
    def channel_messages(name: str, limit: int = Query(50, ge=1, le=500)):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        msgs = ch.tail(limit)
        return {"channel": name, "count": len(msgs), "messages": msgs}

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
        ch = Channel(ch_path, name)
        added = ch.add_member(req.agent_id)
        return {"ok": True, "added": added, "members": ch.list_members()}

    @app.post("/api/channels/{name}/admins")
    def add_admin(name: str, req: AddAdminRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        ch = Channel(ch_path, name)
        added = ch.add_admin(req.agent_id, is_worker=req.is_worker)
        return {
            "ok": True, "added": added,
            "admins": ch.list_admins(),
            "human_admins": ch.list_human_admins(),
        }

    # ====================== Mailboxes ======================

    @app.get("/api/mailboxes/{agent_id}")
    def get_mailbox(agent_id: str):
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if not mb_path.exists():
            raise HTTPException(404, f"mailbox {agent_id} not found")
        mb = Mailbox(mb_path, agent_id)
        return {"agent_id": agent_id, "count": mb.count(), "mails": mb.peek()}

    @app.delete("/api/mailboxes/{agent_id}")
    def clear_mailbox(agent_id: str):
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if not mb_path.exists():
            raise HTTPException(404, f"mailbox {agent_id} not found")
        # 清空用 read_and_clear (atomic)
        cleared = Mailbox(mb_path, agent_id).read_and_clear()
        return {"ok": True, "cleared": len(cleared)}

    # ====================== Sessions ======================

    @app.get("/api/sessions/{agent_id}")
    def list_sessions(agent_id: str):
        sess_path = data_dir / "sessions" / f"{agent_id}.json"
        if not sess_path.exists():
            return {"agent_id": agent_id, "sessions": [], "count": 0}
        sm = SessionManager(sess_path, agent_id)
        sessions = sm.list_all()
        return {
            "agent_id": agent_id,
            "count": len(sessions),
            "sessions": [s.to_dict() for s in sessions],
        }

    @app.get("/api/sessions/{agent_id}/active")
    def active_sessions(agent_id: str):
        sess_path = data_dir / "sessions" / f"{agent_id}.json"
        if not sess_path.exists():
            return {"agent_id": agent_id, "sessions": [], "count": 0}
        sm = SessionManager(sess_path, agent_id)
        sessions = sm.list_active()
        return {
            "agent_id": agent_id,
            "count": len(sessions),
            "sessions": [s.to_dict() for s in sessions],
        }

    @app.post("/api/sessions/{agent_id}/decide")
    def decide_session(agent_id: str, req: DecideRequest):
        """decide_session API (LLM-free, 纯程序化)."""
        sess_path = data_dir / "sessions" / f"{agent_id}.json"
        if not sess_path.exists():
            # 新建空 SM
            sm = SessionManager(sess_path, agent_id)
        else:
            sm = SessionManager(sess_path, agent_id)
        session, is_new = sm.decide_session(
            task_id=req.task_id, topic=req.topic, channel=req.channel,
        )
        return {
            "agent_id": agent_id,
            "is_new": is_new,
            "session": session.to_dict(),
        }

    # ====================== State Board ======================

    @app.get("/api/state_board")
    def get_state_board():
        sb = StateBoard(data_dir / "state_board.json")
        return {"tasks": sb.list_all()}

    @app.get("/api/state_board/{task_id}")
    def get_task_state(task_id: str):
        sb = StateBoard(data_dir / "state_board.json")
        task = sb.get(task_id)
        if not task:
            raise HTTPException(404, f"task {task_id} not found")
        return {"task_id": task_id, "task": task}

    # ====================== Scanner ======================

    @app.get("/api/scanner/status")
    def scanner_status():
        state_file = data_dir / "scanner_state.json"
        if not state_file.exists():
            return {"ok": True, "offsets": {}}
        try:
            data = json.loads(state_file.read_text("utf-8"))
            return {"ok": True, "offsets": data.get("offsets", {}), "updated_at": data.get("updated_at")}
        except (json.JSONDecodeError, OSError):
            return {"ok": False, "offsets": {}}

    @app.post("/api/scanner/start")
    def start_scanner():
        try:
            proc = pm.start_scanner()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "process": proc.to_dict()}

    @app.post("/api/scanner/stop")
    def stop_scanner():
        for p in pm.list_processes(kind="scanner"):
            if p.is_running():
                pm.stop(p.process_id)
        return {"ok": True}

    # ====================== Scheduler ======================

    @app.get("/api/scheduler/status")
    def scheduler_status():
        """Scheduler 运行状态: 有 running scheduler 进程 → True."""
        for p in pm.list_processes(kind="scheduler"):
            if p.is_running():
                return {"running": True, "process": p.to_dict()}
        return {"running": False}

    @app.post("/api/scheduler/start")
    def start_scheduler():
        try:
            proc = pm.start_scheduler()
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        return {"ok": True, "process": proc.to_dict()}

    @app.post("/api/scheduler/stop")
    def stop_scheduler():
        for p in pm.list_processes(kind="scheduler"):
            if p.is_running():
                pm.stop(p.process_id)
        return {"ok": True}

    # ====================== Processes ======================

    @app.get("/api/processes")
    def list_processes():
        pm.cleanup_finished()
        procs = pm.list_processes()
        return {
            "count": len(procs),
            "processes": [p.to_dict() for p in procs],
        }

    @app.get("/api/processes/{process_id}")
    def get_process(process_id: str):
        p = pm.get(process_id)
        if not p:
            raise HTTPException(404, f"process {process_id} not found")
        return p.to_dict()

    @app.post("/api/processes/{process_id}/stop")
    def stop_process(process_id: str):
        if not pm.stop(process_id):
            raise HTTPException(404, f"process {process_id} not found or not running")
        return {"ok": True}

    # ====================== Stats ======================

    @app.get("/api/stats")
    def stats():
        ch_dir = data_dir / "channels"
        mb_dir = data_dir / "mailboxes"
        sess_dir = data_dir / "sessions"
        # 频道消息总数
        total_msgs = 0
        for p in ch_dir.glob("*.jsonl"):
            total_msgs += sum(1 for _ in p.open("rb"))
        # 邮箱邮件总数
        total_mails = 0
        for p in mb_dir.glob("*.json"):
            try:
                total_mails += Mailbox(p, p.stem).count()
            except (OSError, json.JSONDecodeError):
                pass
        # session 总数
        total_sessions = 0
        for p in sess_dir.glob("*.json"):
            try:
                sm = SessionManager(p, p.stem)
                total_sessions += len(sm.list_all())
            except (OSError, json.JSONDecodeError):
                pass
        # 进程
        pm.cleanup_finished()
        agents_running = sum(1 for p in pm.list_processes(kind="agent") if p.is_running())
        scanner_running = pm.is_kind_running("scanner")
        scheduler_running = pm.is_kind_running("scheduler")
        return {
            "channels": len(list(ch_dir.glob("*.jsonl"))),
            "agents": len(list(mb_dir.glob("*.json"))),
            "total_messages": total_msgs,
            "total_mails": total_mails,
            "total_sessions": total_sessions,
            "running": {
                "agents": agents_running,
                "scanner": scanner_running,
                "scheduler": scheduler_running,
            },
        }

    # ====================== Static (WebUI 占位) ======================

    # server.py 在 src/agents_chat/v2/server.py, webui/ 在项目根
    # parent.parent.parent = src/, 再上一层 = 项目根
    webui_dir = Path(__file__).parent.parent.parent.parent / "webui"
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
    parser.add_argument("--reload", action="store_true", help="dev mode auto-reload")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    app = create_app(data_dir=data_dir, host=args.host, port=args.port)

    try:
        import uvicorn
    except ImportError:
        print("ERROR: uvicorn not installed. pip install 'uvicorn[standard]>=0.27'")
        sys.exit(1)

    uvicorn.run(
        app, host=args.host, port=args.port, reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()


__all__ = ["create_app", "main"]
