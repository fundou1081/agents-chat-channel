"""
CLI entry. Subcommands:
- demo    : 跑 3-author demo
- web     : 启动 web UI
- send    : 手动发一封邮件
- status  : 打印所有 author 当前状态
- post    : 发一个 Post (公告/任务/讨论/临时聊天)
- channel : 建/加入/退出/发消息到频道
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .author.base import Author
from .heartbeat import HeartbeatRegistry
from .models import Mail, Persona
from .monitor import Monitor
from .policy import FreeChatManager, NetworkPolicy, RateLimiter
from .storage.channels_db import ChannelDB
from .storage.mailbox_db import MailboxDB
from .storage.posts_db import PostsDB
from .storage.session_db import SessionDB
from .web.server import start_web_server


WORKDIR_BASE = os.environ.get("AGENTCHAT_WORKDIR", "/tmp/agents-chat-workdirs")
QWEN_MODEL = os.environ.get("AGENTCHAT_QWEN_MODEL", "minimax-m2.5:cloud")
OPENCODE_MODEL = os.environ.get("AGENTCHAT_OPENCODE_MODEL", "opencode/minimax-m3-free")

BUILTIN_PERSONAS = {
    "zhang": Persona(
        id="zhang-frontend", display_name="小张", emoji="🎨", title="前端工程师",
        system_prompt="你是一个经验丰富的前端工程师,擅长 React、TypeScript、Tailwind。\n"
                      "你会在工作目录下写代码 (使用 bash, read, write 工具)。",
        workdir=f"{WORKDIR_BASE}/zhang",
        heartbeat_seconds=15, sleep_hours=None,
        llm_backend="opencode", llm_model=OPENCODE_MODEL,
    ),
    "li": Persona(
        id="li-backend", display_name="小李", emoji="⚙️", title="后端工程师",
        system_prompt="你是一个资深后端工程师,擅长 Python、Go、数据库设计。",
        workdir=f"{WORKDIR_BASE}/li",
        heartbeat_seconds=18, sleep_hours=None,
        llm_backend="opencode", llm_model=OPENCODE_MODEL,
    ),
    "pm": Persona(
        id="pm", display_name="林经理", emoji="🎩", title="项目经理",
        system_prompt=(
            "你是 PM (项目经理) — 唯一职责: 拆任务 + 派活 + 汇报。\n"
            "recipients 必须是严格真实 author id (zhang-frontend / li-backend / pm / god)。\n"
            "不写代码不调工具, 总是发 2 封邮件 (派活 + 汇报) 用严格 JSON 输出。\n"
            "outgoing_mail 包含 1+ 封派活 + 1 封汇报, closed_sessions 包含原 thread_id。"
        ),
        workdir=f"{WORKDIR_BASE}/pm",
        heartbeat_seconds=15, sleep_hours=None,
        llm_backend="qwen", llm_model=QWEN_MODEL,
    ),
}


def get_data_dir() -> Path:
    return Path("./data")


def get_mailbox() -> MailboxDB:
    return MailboxDB(get_data_dir() / "mailbox.db")


def get_sessions() -> SessionDB:
    return SessionDB(get_data_dir() / "sessions.db")


def get_monitor() -> Monitor:
    return Monitor(get_data_dir() / "logs" / "monitor.jsonl")


def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_data_dir() / "logs" / "rate_limits.db")


def get_posts_db() -> PostsDB:
    return PostsDB(get_data_dir() / "posts.db")


def get_channels_db() -> ChannelDB:
    return ChannelDB(get_data_dir() / "channels.db")


def get_policy() -> NetworkPolicy:
    return NetworkPolicy(
        max_mails_per_hour=int(os.environ.get("AGENTCHAT_MAX_MAILS_PER_HOUR", "30")),
        max_mails_per_day=int(os.environ.get("AGENTCHAT_MAX_MAILS_PER_DAY", "200")),
        max_actions_per_tick=int(os.environ.get("AGENTCHAT_MAX_ACTIONS_PER_TICK", "3")),
        max_thread_rounds=int(os.environ.get("AGENTCHAT_MAX_THREAD_ROUNDS", "8")),
        min_tick_interval_seconds=int(os.environ.get("AGENTCHAT_MIN_TICK_INTERVAL", "3")),
    )


def make_llm_for_persona(persona) -> object:
    backend = persona.llm_backend
    if backend == "mock":
        from .llm.mock import MockLLM
        return MockLLM()
    elif backend == "qwen":
        from .llm.qwen import QwenAgent
        model = persona.llm_model or "minimax-m2.5:cloud"
        return QwenAgent(
            base_url="http://localhost:11434", model=model, timeout_seconds=60,
        )
    elif backend == "opencode":
        from .llm.opencode import OpenCodeAgent
        model = persona.llm_model or "opencode/minimax-m3-free"
        return OpenCodeAgent(model=model, timeout_seconds=180)
    else:
        raise ValueError(f"Unknown llm_backend: {backend}")


def make_authors(persona_ids, llm=None, registry=None, monitor=None,
                rate_limiter=None, policy=None, posts=None, channels=None):
    mailbox = get_mailbox()
    sessions = get_sessions()
    if monitor is None: monitor = get_monitor()
    if rate_limiter is None: rate_limiter = get_rate_limiter()
    if policy is None: policy = get_policy()
    if posts is None: posts = get_posts_db()
    if channels is None: channels = get_channels_db()

    authors = {}
    for pid in persona_ids:
        if pid not in BUILTIN_PERSONAS:
            print(f"Unknown persona: {pid}")
            continue
        persona = BUILTIN_PERSONAS[pid]
        author_llm = llm if llm is not None else make_llm_for_persona(persona)
        a = Author(
            persona=persona, mailbox=mailbox, sessions=sessions, llm=author_llm,
            data_dir=get_data_dir() / "logs", registry=registry, monitor=monitor,
            rate_limiter=rate_limiter, policy=policy, posts=posts, channels=channels,
        )
        authors[pid] = a
    return authors


# ============================================================================
# Subcommands
# ============================================================================


async def cmd_demo(args):
    print("=" * 60)
    print("🤖 agents-chat-channel demo (3-channel architecture)")
    print("=" * 60)
    print(f"LLM backend: {args.llm}")

    llm = None if args.llm == "auto" else _make_llm(args)
    rl = get_rate_limiter()
    policy = get_policy()
    monitor = get_monitor()
    posts = get_posts_db()
    channels = get_channels_db()
    registry = HeartbeatRegistry(
        policy=policy, rate_limiter=rl, monitor=monitor, posts=posts, channels=channels,
    )
    authors = make_authors(
        ["pm", "zhang", "li"], llm, registry=registry,
        monitor=monitor, rate_limiter=rl, policy=policy, posts=posts, channels=channels,
    )
    for a in authors.values():
        b = a.persona.llm_backend
        m = a.persona.llm_model or "(default)"
        print(f"  {a.persona.id:20s} → {b:10s} {m}")

    for a in authors.values():
        await a.start()

    await asyncio.sleep(3)
    mailbox = get_mailbox()
    task_mail = Mail.new(
        sender="god", recipients=["pm"],
        subject="[任务] 写个 hello.py",
        body="请安排团队: 写一个 hello.py, 有个 hello() 函数返回 'Hello from agents-chat'",
        priority=8, requires_ack=True,
    )
    await mailbox.deliver(task_mail)
    registry.trigger_burst("pm")
    print(f"\n[god] → sent task to PM: {task_mail.subject}")

    duration = getattr(args, "duration", 90)
    print(f"\n[main] running {duration}s...\n")
    for i in range(duration):
        await asyncio.sleep(1)
        if (i + 1) % 15 == 0:
            print(f"--- status @ t={i+1}s ---")
            for snap in registry.snapshots():
                sess_n = len(snap["active_sessions"])
                print(f"  {snap['persona']['emoji']} {snap['persona']['display_name']}: "
                      f"status={snap['status']}, sessions={sess_n}, ticks={snap['total_ticks']}, "
                      f"actions={snap['total_actions']}")

    print(f"\n[main] stopping...")
    await registry.stop_all()

    print(f"\n[main] final state:")
    for snap in registry.snapshots():
        print(f"  {snap['persona']['display_name']}: total_ticks={snap['total_ticks']}, total_actions={snap['total_actions']}")

    import aiosqlite
    async with aiosqlite.connect(get_data_dir() / "mailbox.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT sender, subject, created_at FROM mails ORDER BY created_at")
        rows = await cursor.fetchall()
        for r in rows[:20]:
            print(f"  [{r['created_at'][:19]}] {r['sender']:>15s}: {r['subject'][:50]}")


async def cmd_web(args):
    print("=" * 60)
    print("🤖 agents-chat-channel web UI (3-channel architecture)")
    print("=" * 60)
    print(f"URL: http://localhost:{args.port}")
    print(f"LLM: {args.llm}")

    llm = None if args.llm == "auto" else _make_llm(args)
    rl = get_rate_limiter()
    policy = get_policy()
    monitor = get_monitor()
    posts = get_posts_db()
    channels = get_channels_db()
    registry = HeartbeatRegistry(
        policy=policy, rate_limiter=rl, monitor=monitor, posts=posts, channels=channels,
    )
    authors = make_authors(
        ["pm", "zhang", "li"], llm, registry=registry,
        monitor=monitor, rate_limiter=rl, policy=policy, posts=posts, channels=channels,
    )
    for a in authors.values():
        await a.start()
    await start_web_server(registry, port=args.port)


async def cmd_send(args):
    mailbox = get_mailbox()
    m = Mail.new(sender="god", recipients=[args.to], subject=args.subject, body=args.body, priority=8)
    await mailbox.deliver(m)
    print(f"[god] → sent to {args.to}: {args.subject}")
    print(f"  thread_id: {m.thread_id}, mail_id: {m.id}")


async def cmd_status(args):
    llm = None if args.llm == "auto" else _make_llm(args)
    rl = get_rate_limiter()
    policy = get_policy()
    monitor = get_monitor()
    posts = get_posts_db()
    channels = get_channels_db()
    registry = HeartbeatRegistry(
        policy=policy, rate_limiter=rl, monitor=monitor, posts=posts, channels=channels,
    )
    authors = make_authors(
        ["pm", "zhang", "li"], llm, registry=registry,
        monitor=monitor, rate_limiter=rl, policy=policy, posts=posts, channels=channels,
    )
    for snap in registry.snapshots():
        print(json.dumps(snap, ensure_ascii=False, indent=2))


def _make_llm(args) -> object:
    backend = getattr(args, "llm", "mock")
    if backend == "mock":
        from .llm.mock import MockLLM
        return MockLLM()
    elif backend == "opencode":
        from .llm.opencode import OpenCodeAgent
        model = getattr(args, "model", None) or OPENCODE_MODEL
        return OpenCodeAgent(model=model, timeout_seconds=180)
    elif backend == "qwen":
        from .llm.qwen import QwenAgent
        model = getattr(args, "model", None) or QWEN_MODEL
        base_url = getattr(args, "base_url", None) or "http://localhost:11434"
        return QwenAgent(model=model, base_url=base_url, timeout_seconds=120)
    else:
        raise ValueError(f"Unknown LLM backend: {backend}")


def cli():
    parser = argparse.ArgumentParser(prog="agents-chat")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="Run end-to-end demo")
    p_demo.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto")
    p_demo.add_argument("--model", default=None)
    p_demo.add_argument("--base-url", default=None)
    p_demo.add_argument("--duration", type=int, default=90)

    p_web = sub.add_parser("web", help="Start web UI")
    p_web.add_argument("--port", type=int, default=7331)
    p_web.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto")
    p_web.add_argument("--model", default=None)
    p_web.add_argument("--base-url", default=None)

    p_send = sub.add_parser("send", help="Send a mail to an author")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)

    p_status = sub.add_parser("status", help="One-shot status of all authors")
    p_status.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto")
    p_status.add_argument("--model", default=None)
    p_status.add_argument("--base-url", default=None)

    args = parser.parse_args()
    cmd_map = {"demo": cmd_demo, "web": cmd_web, "send": cmd_send, "status": cmd_status}
    asyncio.run(cmd_map[args.cmd](args))


if __name__ == "__main__":
    cli()
