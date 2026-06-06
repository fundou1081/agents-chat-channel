"""
CLI entry. Subcommands:
- demo    : 跑 2-author demo (不接 web)
- web     : 启动 web UI
- send    : 手动发一封邮件给某个 author
- status  : 打印所有 author 当前状态
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .author.base import Author
from .heartbeat import HeartbeatRegistry
from .llm.mock import MockLLM
from .models import Mail, Persona
from .storage.mailbox_db import MailboxDB
from .storage.session_db import SessionDB
from .web.server import start_web_server


# 一些内置 personas
BUILTIN_PERSONAS = {
    "zhang": Persona(
        id="zhang-frontend",
        display_name="小张",
        emoji="🎨",
        title="前端工程师",
        system_prompt="你是一个经验丰富的前端工程师,擅长 React、TypeScript、Tailwind。",
        workdir="/tmp/zhang",
        heartbeat_seconds=10,
        sleep_hours=None,  # 24/7 在线 (demo)
    ),
    "li": Persona(
        id="li-backend",
        display_name="小李",
        emoji="⚙️",
        title="后端工程师",
        system_prompt="你是一个资深后端工程师,擅长 Python、Go、数据库设计。",
        workdir="/tmp/li",
        heartbeat_seconds=12,
        sleep_hours=None,
    ),
    "pm": Persona(
        id="pm",
        display_name="林经理",
        emoji="🎩",
        title="项目经理",
        system_prompt="你是 PM,负责拆任务、派活、review。",
        workdir="/tmp/pm",
        heartbeat_seconds=8,
        sleep_hours=None,
    ),
}


def get_data_dir() -> Path:
    return Path("./data")


def get_mailbox() -> MailboxDB:
    return MailboxDB(get_data_dir() / "mailbox.db")


def get_sessions() -> SessionDB:
    return SessionDB(get_data_dir() / "sessions.db")


def make_authors(persona_ids: list[str], llm: MockLLM, registry: "HeartbeatRegistry | None" = None) -> dict[str, Author]:
    """根据 persona id 创建 author。"""
    mailbox = get_mailbox()
    sessions = get_sessions()
    authors = {}
    for pid in persona_ids:
        if pid not in BUILTIN_PERSONAS:
            print(f"Unknown persona: {pid}. Available: {list(BUILTIN_PERSONAS.keys())}")
            continue
        a = Author(
            persona=BUILTIN_PERSONAS[pid],
            mailbox=mailbox,
            sessions=sessions,
            llm=llm,
            data_dir=get_data_dir() / "logs",
            registry=registry,
        )
        authors[pid] = a
    return authors


# =============================================================================
# Subcommands
# =============================================================================


async def cmd_demo(args):
    """跑 demo: 2-3 author 互相发邮件。"""
    print("=" * 60)
    print("🤖 agents-chat-channel demo")
    print("=" * 60)
    print("Authors: zhang (前端), li (后端), pm (PM)")
    print("Demo: god 发任务给 PM, PM 派给 zhang + li, 它们互相协作")
    print()

    llm = MockLLM()
    authors = make_authors(["pm", "zhang", "li"], llm)
    registry = HeartbeatRegistry()
    for a in authors.values():
        registry.register(a)

    # 启动所有 author
    await registry.start_all()
    print(f"\n[god] → starting demo. Will send task to PM in 3s...\n")

    await asyncio.sleep(3)

    # 模拟上帝发邮件
    mailbox = get_mailbox()
    task_mail = Mail.new(
        sender="god",
        recipients=["pm"],
        subject="[任务] 做一个用户登录页",
        body="请拆解任务,前端登录页 + 后端 auth API + 单元测试。预计 1 小时。",
        priority=8,
        requires_ack=True,
    )
    await mailbox.deliver(task_mail)
    registry.trigger_burst("pm")
    print(f"[god] → sent task to PM: {task_mail.subject}")

    # 让它跑 60s
    print(f"\n[main] running 60s...\n")
    for i in range(60):
        await asyncio.sleep(1)
        if (i + 1) % 10 == 0:
            print(f"--- status @ t={i+1}s ---")
            for snap in registry.snapshots():
                sess_n = len(snap["active_sessions"])
                print(f"  {snap['persona']['emoji']} {snap['persona']['display_name']}: "
                      f"status={snap['status']}, sessions={sess_n}, ticks={snap['total_ticks']}")

    # 停止
    print(f"\n[main] stopping...")
    await registry.stop_all()

    # 打印最终状态
    print(f"\n[main] final state:")
    for snap in registry.snapshots():
        print(f"  {snap['persona']['display_name']}: total_ticks={snap['total_ticks']}, "
              f"total_actions={snap['total_actions']}")

    # 打印 mailbox
    print(f"\n[main] mailbox dump:")
    async with __import__("aiosqlite").connect(get_data_dir() / "mailbox.db") as db:
        db.row_factory = __import__("aiosqlite").Row
        cursor = await db.execute("SELECT sender, subject, created_at FROM mails ORDER BY created_at")
        rows = await cursor.fetchall()
        for r in rows[:20]:
            print(f"  [{r['created_at'][:19]}] {r['sender']:>15s}: {r['subject'][:50]}")


async def cmd_web(args):
    """启动 web UI."""
    print("=" * 60)
    print("🤖 agents-chat-channel web UI")
    print("=" * 60)
    print(f"URL: http://localhost:{args.port}")
    print("=" * 60)

    llm = MockLLM()
    authors = make_authors(["pm", "zhang", "li"], llm)
    registry = HeartbeatRegistry()
    for a in authors.values():
        registry.register(a)
    await registry.start_all()

    await start_web_server(registry, port=args.port)


async def cmd_send(args):
    """手动发邮件给某个 author。"""
    mailbox = get_mailbox()
    m = Mail.new(
        sender="god",
        recipients=[args.to],
        subject=args.subject,
        body=args.body,
        priority=8,
    )
    await mailbox.deliver(m)
    print(f"[god] → sent to {args.to}: {args.subject}")
    print(f"  body: {args.body}")
    print(f"  thread_id: {m.thread_id}")
    print(f"  mail_id: {m.id}")


async def cmd_status(args):
    """打印所有 author 状态 (一次性,不启动 heartbeat)."""
    llm = MockLLM()
    authors = make_authors(["pm", "zhang", "li"], llm)
    registry = HeartbeatRegistry()
    for a in authors.values():
        registry.register(a)
    for snap in registry.snapshots():
        print(json.dumps(snap, ensure_ascii=False, indent=2))


# =============================================================================
# Main
# =============================================================================


def cli():
    parser = argparse.ArgumentParser(prog="agents-chat")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="Run end-to-end demo (no web)")

    p_web = sub.add_parser("web", help="Start web UI")
    p_web.add_argument("--port", type=int, default=7331)

    p_send = sub.add_parser("send", help="Send a mail to an author")
    p_send.add_argument("--to", required=True, help="Recipient author id")
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)

    sub.add_parser("status", help="One-shot status of all authors")

    args = parser.parse_args()
    cmd_map = {
        "demo": cmd_demo,
        "web": cmd_web,
        "send": lambda a: cmd_send(a),
        "status": cmd_status,
    }
    asyncio.run(cmd_map[args.cmd](args))


if __name__ == "__main__":
    cli()
