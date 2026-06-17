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
  python -m agents_chat.server --port 8765 --data-dir ./data_v2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Body, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..infra.files.channel import Channel
from ..infra.files.mailbox import Mailbox
from ..core.session_manager import SessionManager
from ..infra.state_board import StateBoard


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


class RunWorkflowRequest(BaseModel):
    yaml_path: str  # YAML 文件路径 (相对于 data_dir 或绝对路径)
    from_stage: Optional[str] = None
    single_stage: Optional[str] = None


def create_app(data_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> FastAPI:
    """构造 FastAPI app."""

    data_dir = data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["channels", "mailboxes", "sessions", "locks"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        print(f"[server] ▶ http://{host}:{port}  data_dir={data_dir}")

        # 启 busd daemon (跟 server 同生命周期)
        busd_proc = None
        try:
            import subprocess
            from .busd import DEFAULT_SOCK_NAME
            sock_path = data_dir / DEFAULT_SOCK_NAME
            busd_proc = subprocess.Popen(
                [sys.executable, "-m", "agents_chat.infra.busd",
                 "--data-dir", str(data_dir),
                 "--socket", str(sock_path)],
                stdout=subprocess.DEVNULL,  # 不输出到 server stdout
                stderr=subprocess.DEVNULL,
            )
            # 等 busd 写 path 文件 (短轮询, max 2s)
            for _ in range(20):
                if (data_dir / "busd.sock.path").exists():
                    break
                time.sleep(0.1)
            print(f"[server] busd spawned (pid={busd_proc.pid}, sock={sock_path})")
        except Exception as e:
            print(f"[server] busd spawn failed: {e} (降级: 仅 watchdog + poll)")
            busd_proc = None

        try:
            yield
        finally:
            # 关闭时: 先关 busd, 再清理 socket path
            if busd_proc is not None and busd_proc.poll() is None:
                try:
                    busd_proc.terminate()
                    busd_proc.wait(timeout=2.0)
                except Exception:
                    try:
                        busd_proc.kill()
                    except Exception:
                        pass
            # 清理 path 文件
            try:
                (data_dir / "busd.sock.path").unlink()
            except OSError:
                pass
            print("[server] ⏹ stopped (busd cleaned)")

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
            msgs = ch.tail(1)
            channels.append({
                "name": name,
                "messages": len(ch),
                "members": ch.list_members(),
                "admins": ch.list_admins(),
                "human_admins": ch.list_human_admins(),
                "enabled_workers": ch.list_enabled_workers(),
                "max_messages": ch.max_messages,
            })
        return channels

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
            "enabled_workers": ch.list_enabled_workers(),
            "max_messages": ch.max_messages,
        }

    @app.post("/api/channels/{name}/members")
    def add_member(name: str, req: AddMemberRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        ch.add_member(req.agent_id)
        return {"ok": True}

    @app.delete("/api/channels/{name}/members/{agent_id}")
    def remove_member(name: str, agent_id: str):
        """从频道中移除成员。"""
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        removed = ch.remove_member(agent_id)
        if not removed:
            raise HTTPException(404, f"member {agent_id} not found in channel {name}")
        return {"ok": True, "removed": agent_id}

    @app.post("/api/channels/{name}/admins")
    def add_admin(name: str, req: AddAdminRequest):
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        ch = Channel(ch_path, name)
        ch.add_admin(req.agent_id, is_worker=not req.is_human)
        return {"ok": True}

    @app.put("/api/channels/{name}/config")
    def update_channel_config(name: str, body: dict = Body(...)):
        """更新频道配置 (统一入口).
        
        支持字段:
          max_messages: int — 最大消息数 (0=不限制)
          enabled_workers: list[str] — worker 白名单
          add_admins: list[str] — 添加管理员
          add_members: list[str] — 添加成员
          remove_members: list[str] — 移除成员
        """
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        
        ch = Channel(ch_path, name)
        
        results = {"ok": True, "channel": name}
        
        # 更新 max_messages
        if "max_messages" in body:
            new_max = int(body["max_messages"])
            if new_max < 0:
                raise HTTPException(400, "max_messages must be >= 0")
            ch.max_messages = new_max
            meta = ch._load_meta()
            meta["max_messages"] = new_max
            ch._save_meta(meta)
            results["max_messages"] = new_max
        
        # 更新 worker 白名单
        if "enabled_workers" in body:
            workers = list(body["enabled_workers"])
            ch.set_enabled_workers(workers)
            results["enabled_workers"] = workers
        
        # 添加管理员
        if "add_admins" in body:
            added = []
            for aid in body["add_admins"]:
                ch.add_admin(aid)
                added.append(aid)
            results["add_admins"] = added
        
        # 添加成员
        if "add_members" in body:
            added = []
            for mid in body["add_members"]:
                ch.add_member(mid)
                added.append(mid)
            results["add_members"] = added
        
        # 移除成员
        if "remove_members" in body:
            removed = []
            for mid in body["remove_members"]:
                ch.remove_member(mid)
                removed.append(mid)
            results["remove_members"] = removed
        
        return results

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
                "workspace_dir": str(data_dir / "workspaces" / agent_id),
            })
        return agents

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

    @app.get("/api/agents/{agent_id}/config")
    def get_agent_config(agent_id: str):
        """从 workspace/config.json 读取 Worker 配置."""
        import json
        
        workspace_dir = data_dir / "workspaces" / agent_id
        config_path = workspace_dir / "config.json"
        
        if not config_path.exists():
            raise HTTPException(404, f"Worker {agent_id} config.json not found")
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            
            return {
                "agent_id": agent_id,
                "config": config,
                "workspace_dir": str(workspace_dir)
            }
        except json.JSONDecodeError as e:
            raise HTTPException(500, f"Invalid JSON in config.json: {str(e)}")

    @app.get("/api/agents/{agent_id}/pdr-status")
    def get_agent_pdr_status(agent_id: str):
        """获取 Worker 的 PDR 四组件状态."""
        from pathlib import Path as PathLib
        
        # Perceive: CommunicationComponent 状态
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        mb = Mailbox(mb_path, agent_id)
        pending_mails = mb.peek()
        
        # 检查订阅频道 (从 workspace 或配置文件读取)
        workspace_dir = data_dir / "workspaces" / agent_id
        subscriptions = []
        if workspace_dir.exists():
            subs_file = workspace_dir / "subscriptions.json"
            if subs_file.exists():
                try:
                    import json
                    subscriptions = json.loads(subs_file.read_text())
                except:
                    pass
        
        # Decide: EventHandler 状态 (从日志或状态文件推断)
        log_path = data_dir / "logs" / f"{agent_id}.log"
        last_decision = None
        if log_path.exists():
            try:
                lines = log_path.read_text().splitlines()
                for line in reversed(lines[-50:]):  # 检查最后50行
                    if "decide_session" in line or "decide_speak" in line:
                        last_decision = line.strip()
                        break
            except:
                pass
        
        # Remember: SessionManager 状态
        sessions_dir = data_dir / "sessions"
        sm = SessionManager(sessions_dir / f"{agent_id}.json", agent_id)
        active_sessions = [s.to_dict() for s in sm.list_active()]
        session_snapshots = []
        try:
            session_snapshots = [s.snapshot() for s in sm.list_active()]
        except:
            pass
        
        # Act: CLI 状态
        cli_type = "unknown"
        model = "unknown"
        
        # 优先从 config.json 读取 CLI 类型
        config_path = workspace_dir / "config.json"
        if config_path.exists():
            try:
                import json
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                cli_type = config.get("cli", "unknown")
            except:
                pass
        
        # 如果 config.json 中没有，fallback 到检查 .md 文件
        if cli_type == "unknown" and workspace_dir.exists():
            for cli_name in ["opencode", "qwen", "mock"]:
                cli_file = workspace_dir / f"{cli_name}.md"
                if cli_file.exists():
                    cli_type = cli_name
                    break
        
        # 尝试从 config.json 或 .md 文件中提取 model 信息
        if cli_type != "unknown":
            # 先尝试从 config.json 读取
            if config_path.exists():
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        config = json.load(f)
                    if "model" in config:
                        model = config["model"]
                except:
                    pass
            
            # 如果没有，尝试从 .md 文件中提取
            if model == "unknown":
                cli_file = workspace_dir / f"{cli_type}.md"
                if cli_file.exists():
                    try:
                        content = cli_file.read_text()
                        if "model" in content.lower():
                            import re
                            match = re.search(r'model[:\s]+([\w-]+)', content, re.IGNORECASE)
                            if match:
                                model = match.group(1)
                    except:
                        pass
        
        # 最后执行时间 (从日志推断)
        last_execution = None
        if log_path.exists():
            try:
                lines = log_path.read_text().splitlines()
                for line in reversed(lines[-20:]):
                    if "execute" in line.lower() or "cli" in line.lower():
                        # 提取时间戳
                        import re
                        time_match = re.search(r'(\d{2}:\d{2}:\d{2})', line)
                        if time_match:
                            last_execution = time_match.group(1)
                            break
            except:
                pass
        
        return {
            "agent_id": agent_id,
            "pdr": {
                "perceive": {
                    "pending_mails_count": len(pending_mails),
                    "pending_mails": pending_mails[:5],  # 最近5封
                    "subscriptions": subscriptions,
                    "last_poll": None  # TODO: 需要更精确的实现
                },
                "decide": {
                    "mode": "proactive" if subscriptions else "passive",
                    "last_decision": last_decision,
                    "decision_history": []  # TODO: 需要持久化决策历史
                },
                "remember": {
                    "active_sessions_count": len(active_sessions),
                    "active_sessions": active_sessions,
                    "session_snapshots": session_snapshots
                },
                "act": {
                    "cli_type": cli_type,
                    "model": model,
                    "workspace_dir": str(workspace_dir) if workspace_dir.exists() else None,
                    "last_execution": last_execution
                }
            }
        }

    @app.post("/api/agents/{agent_id}/start")
    def start_agent(agent_id: str):
        """启动 worker 进程 (后台异步执行,立即返回)."""
        import subprocess, sys
        log_path = data_dir / "logs" / f"{agent_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(
            [sys.executable, "-m", "agents_chat.main",
             "--data-dir", str(data_dir),
             "run-worker", agent_id],
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
        )
        return {"ok": True, "process": {"agent_id": agent_id, "pid": p.pid}}

    @app.post("/api/agents/{agent_id}/create")
    def create_agent(agent_id: str, body: dict = Body(...)):
        """创建新的 worker，可选择使用已有 workspace 或创建新的。"""
        from ..infra.worker_factory import _init_workspace
        
        cli_type = body.get("cli_type", "mock")
        use_existing_workspace = body.get("use_existing_workspace", False)
        existing_workspace_name = body.get("existing_workspace_name", "")
        role = body.get("role", "")
        system_prompt = body.get("system_prompt", "")
        skills = body.get("skills", [])
        mcp_servers = body.get("mcp_servers", [])
        subscriptions = body.get("subscriptions", [])  # 订阅频道列表
        
        # 确定 workspace 目录
        if use_existing_workspace and existing_workspace_name:
            # 使用已有的 workspace
            ws_dir = data_dir / "workspaces" / existing_workspace_name
            if not ws_dir.exists():
                raise HTTPException(404, f"Workspace '{existing_workspace_name}' not found")
        else:
            # 创建新的 workspace
            ws_dir = data_dir / "workspaces" / agent_id
            _init_workspace(
                workspace_dir=ws_dir,
                cli_name=cli_type,
                role=role or agent_id,
                system_prompt=system_prompt,
                skills=skills if skills else None,
                mcp_servers=mcp_servers if mcp_servers else None,
                role_template="",
                use_default_prompt=True,
                subscriptions=subscriptions if subscriptions else None,  # 传递订阅列表
            )
        
        # 创建 mailbox
        mb_path = data_dir / "mailboxes" / f"{agent_id}.json"
        if not mb_path.exists():
            mb_path.write_text(
                json.dumps({"agent": agent_id, "pending": []}, ensure_ascii=False),
                encoding="utf-8"
            )
        
        # 如果有订阅，保存到 subscriptions.json
        if subscriptions:
            workspace_dir = data_dir / "workspaces" / agent_id
            subs_file = workspace_dir / "subscriptions.json"
            subs_file.write_text(json.dumps(subscriptions, ensure_ascii=False), encoding="utf-8")
        
        # 更新 config.json，添加 subscriptions 字段（移除 mode）
        config_path = ws_dir / "config.json"
        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                # 移除 mode 字段（如果存在）
                config.pop("mode", None)
                # 添加 subscriptions
                if subscriptions:
                    config["subscriptions"] = subscriptions
                elif "subscriptions" in config:
                    config.pop("subscriptions", None)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[WARN] Failed to update config.json: {e}")
        
        return {
            "ok": True,
            "agent_id": agent_id,
            "workspace": str(ws_dir),
            "used_existing": use_existing_workspace and existing_workspace_name
        }

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

    @app.get("/api/agents/{agent_id}/workspace")
    def get_agent_workspace(agent_id: str):
        """列出 agent 的 workspace 内容."""
        ws_dir = data_dir / "workspaces" / agent_id
        if not ws_dir.exists():
            return {"agent_id": agent_id, "workspace_dir": str(ws_dir), "files": [], "exists": False}
        files = []
        for f in ws_dir.rglob("*"):
            if f.is_file():
                rel = f.relative_to(ws_dir)
                size = f.stat().st_size
                files.append({"path": str(rel), "size": size})
        return {"agent_id": agent_id, "workspace_dir": str(ws_dir), "files": files, "exists": True}

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
        return {"sessions": [s.to_dict() for s in sm.list_all()]}

    @app.get("/api/sessions/{agent_id}/active")
    def get_active_sessions(agent_id: str):
        sessions_dir = data_dir / "sessions"
        sm = SessionManager(sessions_dir / f"{agent_id}.json", agent_id)
        return {"sessions": [s.to_dict() for s in sm.list_active()]}

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

    @app.get("/api/workspaces")
    def list_workspaces():
        """列出所有可用的 workspace 目录."""
        ws_dir = data_dir / "workspaces"
        if not ws_dir.exists():
            return {"workspaces": []}
        
        workspaces = []
        for d in ws_dir.iterdir():
            if d.is_dir():
                # 检查是否有 roles.md
                has_roles = (d / "roles.md").exists()
                # 获取 CLI 类型
                cli_type = "unknown"
                for cli_name in ["opencode", "qwen", "mock"]:
                    if (d / f"{cli_name}.md").exists():
                        cli_type = cli_name
                        break
                
                workspaces.append({
                    "name": d.name,
                    "path": str(d),
                    "has_roles": has_roles,
                    "cli_type": cli_type,
                    "created_at": d.stat().st_mtime
                })
        
        # 按创建时间排序
        workspaces.sort(key=lambda x: x["created_at"], reverse=True)
        return {"workspaces": workspaces}

    @app.get("/api/channels/{name}/member-status")
    def get_channel_member_status(name: str):
        """获取频道成员的实时状态，包括当前 prompt 和处理状态。"""
        ch_path = data_dir / "channels" / f"{name}.jsonl"
        if not ch_path.exists():
            raise HTTPException(404, f"channel {name} not found")
        
        ch = Channel(ch_path, name)
        members = ch.list_members()
        
        member_statuses = []
        for member_id in members:
            # 获取 worker 的 session 状态
            sessions_dir = data_dir / "sessions"
            sm_path = sessions_dir / f"{member_id}.json"
            
            status = {
                "agent_id": member_id,
                "status": "idle",  # idle, processing, waiting
                "current_session": None,
                "current_prompt": None,
                "progress": 0,
                "last_activity": None
            }
            
            if sm_path.exists():
                try:
                    sm = SessionManager(sm_path, member_id)
                    active_sessions = sm.list_active()
                    
                    if active_sessions:
                        # 取第一个活跃 session
                        session = active_sessions[0]
                        status["status"] = "processing"
                        status["current_session"] = {
                            "session_id": session.session_id,
                            "topic": session.topic,
                            "progress": session.progress,
                            "next_action": session.next_action
                        }
                        status["progress"] = session.progress
                        
                        # 尝试从 workspace 获取当前 prompt
                        workspace_dir = data_dir / "workspaces" / member_id
                        if workspace_dir.exists():
                            # 检查是否有正在执行的 prompt 文件
                            prompt_file = workspace_dir / "current_prompt.txt"
                            if prompt_file.exists():
                                status["current_prompt"] = prompt_file.read_text()[:500]  # 限制长度
                    else:
                        status["status"] = "idle"
                        
                except Exception as e:
                    print(f"Error getting status for {member_id}: {e}")
            
            member_statuses.append(status)
        
        return {
            "channel": name,
            "members": member_statuses,
            "total_members": len(members)
        }

    # -------------------------------------------------------------------------
    # Workflow API (Stage-Isolated)
    # -------------------------------------------------------------------------

    @app.get("/api/workflows")
    def list_workflow_runs(limit: int = Query(20, ge=1, le=100)):
        """列所有 workflow runs (最近 N 条)."""
        runs_dir = data_dir / "runs"
        if not runs_dir.is_dir():
            return {"runs": []}
        run_files = sorted(
            runs_dir.glob("run-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        result = []
        for rf in run_files[:limit]:
            try:
                data = json.loads(rf.read_text("utf-8"))
                result.append(data)
            except (json.JSONDecodeError, KeyError):
                pass
        return {"runs": result}

    # 注意: 这个端点必须在 /api/workflows/{run_id} 之前定义
    # (FastAPI 按定义顺序匹配, "active" 会被 catch-all 吃掉)
    @app.get("/api/workflows/active")
    def list_active_workflows():
        """列所有 active (running) workflow run IDs."""
        from ..workflow.registry import WorkflowRegistry
        registry = WorkflowRegistry.get_default()
        return {"active": registry.list_active()}

    @app.get("/api/workflows/{run_id}")
    def get_workflow_run(run_id: str):
        """获取单个 workflow run 的详细状态."""
        run_file = data_dir / "runs" / f"{run_id}.json"
        if not run_file.exists():
            raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")
        try:
            return json.loads(run_file.read_text("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"invalid run file: {e}")

    @app.post("/api/workflows/run")
    async def run_workflow(
        req: RunWorkflowRequest,
        background_tasks: BackgroundTasks,
    ):
        """启动 workflow 跑 (异步, 返 run_id)."""
        from ..workflow.loader import load_workflow
        from ..workflow.scheduler import WorkflowScheduler

        yaml_path = Path(req.yaml_path)
        if not yaml_path.is_absolute():
            yaml_path = data_dir / req.yaml_path
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"YAML not found: {yaml_path}")

        try:
            spec = load_workflow(yaml_path)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(status_code=400, detail=f"invalid workflow: {e}")

        scheduler = WorkflowScheduler(
            spec,
            data_dir=data_dir,
            from_stage=req.from_stage,
            single_stage=req.single_stage,
        )
        # 注册到 global registry (用于 cancel endpoint 查找)
        from ..workflow.registry import WorkflowRegistry
        WorkflowRegistry.get_default().register(scheduler)
        # 在后台跑 (立即返 run_id)
        background_tasks.add_task(_run_workflow_background, scheduler)
        return {
            "run_id": scheduler.run_id,
            "workflow": spec.name,
            "stages": len(spec.stages),
            "from_stage": req.from_stage,
            "single_stage": req.single_stage,
        }

    @app.post("/api/workflows/validate")
    def validate_workflow(req: RunWorkflowRequest):
        """验证 workflow YAML 语法 (不跑)."""
        from ..workflow.loader import load_workflow

        yaml_path = Path(req.yaml_path)
        if not yaml_path.is_absolute():
            yaml_path = data_dir / req.yaml_path
        if not yaml_path.exists():
            raise HTTPException(status_code=404, detail=f"YAML not found: {yaml_path}")

        try:
            spec = load_workflow(yaml_path)
            stages = spec.topological_order()
            return {
                "valid": True,
                "name": spec.name,
                "description": spec.description,
                "stages": [
                    {
                        "id": s.id,
                        "workers": len(s.workers),
                        "depends_on": s.depends_on,
                        "timeout": s.timeout,
                    }
                    for s in stages
                ],
            }
        except (ValueError, FileNotFoundError) as e:
            return {"valid": False, "error": str(e)}

    @app.get("/api/workflows/{run_id}/html")
    def get_workflow_html(run_id: str):
        """获取 workflow run 的 HTML 可视化 (DAG + status)."""
        from ..workflow.loader import load_workflow
        from ..workflow.scheduler import WorkflowRunResult
        from ..workflow.html_report import render_workflow_html

        # 加载 run 数据
        run_file = data_dir / "runs" / f"{run_id}.json"
        if not run_file.exists():
            raise HTTPException(status_code=404, detail=f"run '{run_id}' not found")

        try:
            run_data = json.loads(run_file.read_text("utf-8"))
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"invalid run file: {e}")

        # 从 run 数据反推 WorkflowSpec (简化版)
        result = WorkflowRunResult(
            workflow_name=run_data.get("workflow_name", "?"),
            run_id=run_data["run_id"],
            status=run_data.get("status", "?"),
            started_at=run_data.get("started_at"),
            finished_at=run_data.get("finished_at"),
            failed_stage=run_data.get("failed_stage"),
            stage_states=run_data.get("stage_states", {}),
        )

        # HTML: 需要 spec → 从 stage_deps 重建完整 DAG (含边)
        from ..workflow.schema import WorkflowSpec, StageSpec, WorkerSpec, DeliverableSpec
        stage_deps = run_data.get("stage_deps", {})
        stage_ids = list(run_data.get("stage_states", {}).keys())
        if not stage_ids:
            raise HTTPException(status_code=400, detail="no stage data in run")
        
        stages = [
            StageSpec(
                id=sid,
                depends_on=stage_deps.get(sid, []),
                workers=[WorkerSpec(id=sid + "-w", cli="mock")],
                deliverable=DeliverableSpec(path="out/x.json"),
            )
            for sid in stage_ids
        ]
        spec = WorkflowSpec(name=run_data.get("workflow_name", "?"), stages=stages)

        html = render_workflow_html(spec, result=result)
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html)

    @app.post("/api/workflows/{run_id}/cancel")
    def cancel_workflow_run(run_id: str):
        """取消运行中的 workflow. R8 修复."""
        from ..workflow.registry import WorkflowRegistry
        registry = WorkflowRegistry.get_default()
        scheduler = registry.get(run_id)
        if not scheduler:
            raise HTTPException(
                status_code=404,
                detail=f"run '{run_id}' not found in active registry (may have already finished)",
            )
        scheduler.cancel()
        return {
            "run_id": run_id,
            "status": "canceled",
            "message": "cancel signal sent; current stage will cleanup and run will return",
        }


    # -------------------------------------------------------------------------
    # WebUI Static
    # -------------------------------------------------------------------------

    # WebUI 在 v2 包内: src/agents_chat/v2/webui/
    webui_dir = Path(__file__).parent.parent / "webui"
    if webui_dir.exists():
        app.mount("/webui", StaticFiles(directory=str(webui_dir), html=True), name="webui")

    return app


async def _run_workflow_background(scheduler):
    """后台任务: 跑 workflow 并记录结果."""
    from ..workflow.scheduler import WorkflowScheduler
    from ..workflow.registry import WorkflowRegistry
    try:
        await scheduler.run()
    finally:
        # 总是从 registry 注销 (不论成功 / 失败 / 取消)
        WorkflowRegistry.get_default().unregister(scheduler.run_id)


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