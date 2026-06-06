"""
CLI entry. Subcommands:
- demo    : 跑 3-author demo (不接 web)
- web     : 启动 web UI
- send    : 手动发一封邮件给某个 author
- status  : 打印所有 author 当前状态
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
from .storage.mailbox_db import MailboxDB
from .storage.session_db import SessionDB
from .web.server import start_web_server


WORKDIR_BASE = os.environ.get("AGENTCHAT_WORKDIR", "/tmp/agents-chat-workdirs")

# 默认 backend 模型 (可用 env 覆盖)
QWEN_MODEL = os.environ.get("AGENTCHAT_QWEN_MODEL", "minimax-m2.5:cloud")
OPENCODE_MODEL = os.environ.get("AGENTCHAT_OPENCODE_MODEL", "opencode/minimax-m3-free")

# 一些内置 personas
BUILTIN_PERSONAS = {
    "zhang": Persona(
        id="zhang-frontend",
        display_name="小张",
        emoji="🎨",
        title="前端工程师",
        system_prompt="你是一个经验丰富的前端工程师,擅长 React、TypeScript、Tailwind。\n"
                      "你会在工作目录下写代码 (使用 bash, read, write 工具)。",
        workdir=f"{WORKDIR_BASE}/zhang",
        heartbeat_seconds=15,
        sleep_hours=None,
        llm_backend="opencode",
        llm_model=OPENCODE_MODEL,
    ),
    "li": Persona(
        id="li-backend",
        display_name="小李",
        emoji="⚙️",
        title="后端工程师",
        system_prompt="你是一个资深后端工程师,擅长 Python、Go、数据库设计。\n"
                      "你会在工作目录下写代码 (使用 bash, read, write 工具)。",
        workdir=f"{WORKDIR_BASE}/li",
        heartbeat_seconds=18,
        sleep_hours=None,
        llm_backend="opencode",
        llm_model=OPENCODE_MODEL,
    ),
    "pm": Persona(
        id="pm",
        display_name="林经理",
        emoji="🎩",
        title="项目经理",
        system_prompt=(
            "你是 PM (项目经理) — 唯一职责: 拆任务 + 派活 + 汇报。\n"
            "\n"
            "**重要规则** (严格遵守):\n"
            "1. 你不写代码, 不调任何工具。\n"
            "2. recipients 字段必须是**严格的真实 author id**, 字面匹配, 不允许变体:\n"
            "   - 'zhang-frontend' (前端, UI, 写 Python 脚本)\n"
            "   - 'li-backend' (后端, API, 逻辑)\n"
            "   - 'pm' (你自己, 一般不用)\n"
            "   - 'god' (上级, 只用于回信汇报)\n"
            "   **绝不要** 用 'dev' / 'developer' / 'team' / 'engineer' / '程序员' / '前端工程师'\n"
            "3. 总是发 2 封邮件: 1 封派活 + 1 封汇报。\n"
            "4. closed_sessions 包含原 thread_id。\n"
            "5. output 严格 JSON, 字段: thinking / outgoing_mail / closed_sessions / next_status。\n"
            "\n"
            "=== 示例 1: UI 任务 (前端) ===\n"
            "inbox: from=god, subject='写个前端 UI'\n"
            '{\n'
            '  "thinking": "前端任务, 派给 zhang-frontend",\n'
            '  "outgoing_mail": [\n'
            '    {"recipients": ["zhang-frontend"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "[子任务] 前端 UI", "body": "请实现...", "priority": 5, "requires_ack": false},\n'
            '    {"recipients": ["god"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "Re: 写个前端 UI", "body": "已派活", "priority": 5, "requires_ack": false}\n'
            '  ],\n'
            '  "closed_sessions": ["<原 thread_id>"],\n'
            '  "next_status": "working"\n'
            '}\n'
            "\n"
            "=== 示例 2: 写 hello.py (前端) ===\n"
            "inbox: from=god, subject='写个 hello.py', body='需要 hello() 函数'\n"
            '{\n'
            '  "thinking": "写 Python 文件, 派给 zhang-frontend (他会用工具)",\n'
            '  "outgoing_mail": [\n'
            '    {"recipients": ["zhang-frontend"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "[子任务] hello.py", "body": "请在 workdir 写 hello.py, 包含 hello() 函数", "priority": 5, "requires_ack": false},\n'
            '    {"recipients": ["god"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "Re: 写个 hello.py", "body": "已派活", "priority": 5, "requires_ack": false}\n'
            '  ],\n'
            '  "closed_sessions": ["<原 thread_id>"],\n'
            '  "next_status": "working"\n'
            '}\n'
            "\n"
            "=== 示例 3: API 任务 (后端) ===\n"
            "inbox: from=god, subject='实现 auth API'\n"
            '{\n'
            '  "thinking": "API 任务, 派给 li-backend",\n'
            '  "outgoing_mail": [\n'
            '    {"recipients": ["li-backend"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "[子任务] auth API", "body": "请实现...", "priority": 5, "requires_ack": false},\n'
            '    {"recipients": ["god"], "thread_id": "<原 thread_id>", "in_reply_to": "<原 mail_id>", "subject": "Re: auth API", "body": "已派活", "priority": 5, "requires_ack": false}\n'
            '  ],\n'
            '  "closed_sessions": ["<原 thread_id>"],\n'
            '  "next_status": "working"\n'
            '}\n'
        ),
        workdir=f"{WORKDIR_BASE}/pm",
        heartbeat_seconds=15,
        sleep_hours=None,
        llm_backend="qwen",
        llm_model=QWEN_MODEL,
    ),
}


def get_data_dir() -> Path:
    return Path("./data")


def get_mailbox() -> MailboxDB:
    return MailboxDB(get_data_dir() / "mailbox.db")


def get_sessions() -> SessionDB:
    return SessionDB(get_data_dir() / "sessions.db")


def make_authors(persona_ids: list[str], llm=None, registry: "HeartbeatRegistry | None" = None) -> dict[str, Author]:
    """根据 persona id 创建 author。

    如果传了 llm, 所有 author 共享同一个 (override 默认).
    如果没传, 每个 author 用自己的 persona.llm_backend + persona.llm_model.
    """
    mailbox = get_mailbox()
    sessions = get_sessions()
    authors = {}
    for pid in persona_ids:
        if pid not in BUILTIN_PERSONAS:
            print(f"Unknown persona: {pid}. Available: {list(BUILTIN_PERSONAS.keys())}")
            continue
        persona = BUILTIN_PERSONAS[pid]
        # 决定 LLM
        if llm is not None:
            author_llm = llm  # 全局 override
        else:
            author_llm = _make_llm_for_persona(persona)
        a = Author(
            persona=persona,
            mailbox=mailbox,
            sessions=sessions,
            llm=author_llm,
            data_dir=get_data_dir() / "logs",
            registry=registry,
        )
        authors[pid] = a
    return authors


def _make_llm_for_persona(persona) -> object:
    """根据 persona 的 llm_backend / llm_model 创建 LLM。"""
    backend = persona.llm_backend
    if backend == "mock":
        from .llm.mock import MockLLM
        return MockLLM()
    elif backend == "qwen":
        from .llm.qwen import QwenAgent
        model = persona.llm_model or "minimax-m2.5:cloud"
        return QwenAgent(
            base_url="http://localhost:11434",
            model=model,
            timeout_seconds=60,
        )
    elif backend == "opencode":
        from .llm.opencode import OpenCodeAgent
        model = persona.llm_model or "opencode/minimax-m3-free"
        return OpenCodeAgent(
            model=model,
            timeout_seconds=180,
        )
    else:
        raise ValueError(f"Unknown llm_backend: {backend}")


# =============================================================================
# LLM factory
# =============================================================================


def _make_llm(args) -> object:
    """根据 --llm 参数创建 LLM 实例 (给 cmd_demo / cmd_web / cmd_status 显式 override 所有 persona)."""
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
        # 本地 ollama daemon (默认), 不需 key
        base_url = getattr(args, "base_url", None) or "http://localhost:11434"
        return QwenAgent(model=model, base_url=base_url, timeout_seconds=120)
    else:
        raise ValueError(f"Unknown LLM backend: {backend}")


# =============================================================================
# Subcommands
# =============================================================================


async def cmd_demo(args):
    """跑 demo: 3 author 互相发邮件。"""
    print("=" * 60)
    print("🤖 agents-chat-channel demo")
    print("=" * 60)
    print("Authors: zhang (前端), li (后端), pm (PM)")
    print(f"LLM backend: {args.llm}")
    if args.llm == "auto":
        print("auto = 每个 author 用自己的 persona.llm_backend 配置")
    if args.llm == "opencode":
        print("⚠️  opencode 后端: 调 CLI, 真干活 (改文件, 跑命令), 慢")
    print("Demo: god 发任务给 PM, PM 派给 zhang + li, 它们互相协作")
    print()

    llm = None if args.llm == "auto" else _make_llm(args)
    registry = HeartbeatRegistry()
    authors = make_authors(["pm", "zhang", "li"], llm, registry=registry)
    for a in authors.values():
        b = a.persona.llm_backend
        m = a.persona.llm_model or "(default)"
        print(f"  {a.persona.id:20s} → {b:10s} {m}")
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
        subject="[任务] 写个 hello.py",
        body="请安排团队: 写一个 hello.py, 有个 hello() 函数返回 'Hello from agents-chat'\n"
             "要求: 1. 实际写文件 2. 完成后回信告诉我文件位置",
        priority=8,
        requires_ack=True,
    )
    await mailbox.deliver(task_mail)
    registry.trigger_burst("pm")
    print(f"[god] → sent task to PM: {task_mail.subject}")

    # 让它跑
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
    import aiosqlite
    async with aiosqlite.connect(get_data_dir() / "mailbox.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT sender, subject, created_at FROM mails ORDER BY created_at")
        rows = await cursor.fetchall()
        for r in rows[:20]:
            print(f"  [{r['created_at'][:19]}] {r['sender']:>15s}: {r['subject'][:50]}")

    # 打印 zhang 的 workdir
    zhang_dir = BUILTIN_PERSONAS["zhang"].workdir
    if Path(zhang_dir).exists():
        print(f"\n[main] zhang workdir ({zhang_dir}):")
        for f in sorted(Path(zhang_dir).iterdir()):
            print(f"  {f.name} ({f.stat().st_size} bytes)")


async def cmd_web(args):
    """启动 web UI."""
    print("=" * 60)
    print("🤖 agents-chat-channel web UI")
    print("=" * 60)
    print(f"URL: http://localhost:{args.port}")
    print(f"LLM: {args.llm}")
    print("=" * 60)

    llm = None if args.llm == "auto" else _make_llm(args)
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
    llm = None if args.llm == "auto" else _make_llm(args)
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

    p_demo = sub.add_parser("demo", help="Run end-to-end demo (no web)")
    p_demo.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto",
                        help="LLM backend (auto = per-persona config, default)")
    p_demo.add_argument("--model", default=None, help="model id (opencode / qwen)")
    p_demo.add_argument("--base-url", default=None, help="API base url (qwen)")
    p_demo.add_argument("--duration", type=int, default=90, help="demo duration seconds")

    p_web = sub.add_parser("web", help="Start web UI")
    p_web.add_argument("--port", type=int, default=7331)
    p_web.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto",
                        help="LLM backend (auto = per-persona config, default)")
    p_web.add_argument("--model", default=None, help="model id (opencode / qwen)")
    p_web.add_argument("--base-url", default=None, help="API base url (qwen)")

    p_send = sub.add_parser("send", help="Send a mail to an author")
    p_send.add_argument("--to", required=True, help="Recipient author id")
    p_send.add_argument("--subject", required=True)
    p_send.add_argument("--body", required=True)

    p_status = sub.add_parser("status", help="One-shot status of all authors")
    p_status.add_argument("--llm", choices=["mock", "opencode", "qwen", "auto"], default="auto")
    p_status.add_argument("--model", default=None)
    p_status.add_argument("--base-url", default=None)

    args = parser.parse_args()
    cmd_map = {
        "demo": cmd_demo,
        "web": cmd_web,
        "send": cmd_send,
        "status": cmd_status,
    }
    asyncio.run(cmd_map[args.cmd](args))


if __name__ == "__main__":
    cli()
