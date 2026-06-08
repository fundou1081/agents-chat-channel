"""
CLI 入口 for v2.0.

Usage:
  python -m agents_chat.v2.main init [--data-dir DIR]
  python -m agents_chat.v2.main run-worker AGENT_ID [--cli mock|qwen|opencode] [--data-dir DIR]
  python -m agents_chat.v2.main post CHANNEL CONTENT [--from FROM]
  python -m agents_chat.v2.main status [TASK_ID]
  python -m agents_chat.v2.main tail CHANNEL [N]
  python -m agents_chat.v2.main inbox AGENT_ID
  python -m agents_chat.v2.main reset

默认 data_dir: ./data_v2 (可用 AGENTS_CHAT_DATA_DIR 环境变量覆盖)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .agent import Agent
from .cli.mock import MockCLI
from .cli.opencode import OpenCodeCLI
from .cli.qwen import QwenCLI
from .files.channel import Channel
from .files.mailbox import Mailbox
from .state_board import StateBoard


# =============================================================================
# 数据初始化
# =============================================================================


def cmd_init(args):
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    for sub in ["channels", "mailboxes", "sessions", "locks", "logs"]:
        (data_dir / sub).mkdir(parents=True, exist_ok=True)

    (data_dir / "state_board.json").write_text(
        json.dumps({"tasks": {}, "updated_at": ""}, ensure_ascii=False, indent=2)
    )
    # 创建默认频道 general (空的 .jsonl)
    (data_dir / "channels" / "general.jsonl").touch()
    print(f"  初始化完成: {data_dir}")
    print(f"  目录: channels/ mailboxes/ sessions/ locks/ logs/")
    print(f"  状态文件: state_board.json")


# =============================================================================
# Worker
# =============================================================================


def cmd_run_worker(args):
    data_dir = Path(args.data_dir).resolve()
    agent_id = args.agent_id
    cli_name = args.cli or "mock"

    cli_map = {"mock": MockCLI, "qwen": QwenCLI, "opencode": OpenCodeCLI}
    cli_cls = cli_map.get(cli_name, MockCLI)

    print(f"[{agent_id}]启动 worker (cli={cli_name})")
    agent = Agent(agent_id=agent_id, cli=cli_cls(), data_dir=data_dir)
    asyncio.run(agent.run())


# =============================================================================
# Helpers
# =============================================================================


def cmd_post(args):
    data_dir = Path(args.data_dir).resolve()
    ch_path = data_dir / "channels" / f"{args.channel}.jsonl"
    if not ch_path.exists():
        print(f"频道 {args.channel} 不存在,先 init", file=sys.stderr)
        sys.exit(1)
    ch = Channel(ch_path, args.channel)
    content = args.content
    msg_type = "text"
    task_id = ""
    # 检测 task broadcast
    import re
    m = re.match(r"\[TASK (\w+)\]\s*(.*)", content)
    if m:
        task_id = m.group(1)
        content = m.group(2)
        msg_type = "task_broadcast"
    msg_id = ch.append(
        from_=args.from_ or "god",
        content=content,
        type=msg_type,
        task_id=task_id or None,
    )
    print(f"已发送: {msg_id} [{msg_type}]")


def cmd_status(args):
    data_dir = Path(args.data_dir).resolve()
    sb = StateBoard(data_dir / "state_board.json")
    if args.task_id:
        task = sb.get_task(args.task_id)
        if not task:
            print(f"task {args.task_id} not found")
        else:
            print(json.dumps(task, ensure_ascii=False, indent=2))
    else:
        data = sb._read_unlocked()
        tasks = data.get("tasks", {})
        if not tasks:
            print("(empty)")
        else:
            print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_tail(args):
    data_dir = Path(args.data_dir).resolve()
    ch_path = data_dir / "channels" / f"{args.channel}.jsonl"
    if not ch_path.exists():
        print(f"频道 {args.channel} 不存在", file=sys.stderr)
        sys.exit(1)
    ch = Channel(ch_path, args.channel)
    msgs = ch.tail(n=args.n2 or args.n or 10)
    if not msgs:
        print("(empty)")
        return
    for m in msgs:
        ts_str = m["ts"]
        try:
            from datetime import datetime
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone()
            ts_fmt = ts.strftime("%H:%M:%S")
        except Exception:
            ts_fmt = ts_str[:8] if len(ts_str) > 8 else ts_str
        print(f"[{ts_fmt}] {m['from']}: {m['content'][:80]}")


def cmd_inbox(args):
    data_dir = Path(args.data_dir).resolve()
    mb_path = data_dir / "mailboxes" / f"{args.agent_id}.json"
    mb = Mailbox(mb_path, args.agent_id)
    mails = mb.peek()
    if not mails:
        print("(empty)")
        return
    for m in mails:
        print(f"  from={m.get('from')} subject={m.get('subject')[:50]}")


def cmd_reset(args):
    data_dir = Path(args.data_dir).resolve()
    for sub in ["mailboxes", "sessions"]:
        d = data_dir / sub
        if d.exists():
            for f in d.glob("*.json"):
                f.write_text(
                    json.dumps({"agent": f.stem, "pending" if sub == "mailboxes" else "sessions": []}, ensure_ascii=False),
                    encoding="utf-8"
                )
    sb = StateBoard(data_dir / "state_board.json")
    sb._write({"tasks": {}, "updated_at": ""})
    print("重置完成 (channels保留)")


# =============================================================================
# 主入口
# =============================================================================


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="agents-chat-channel v2.0 CLI")
    parser.add_argument("--data-dir", default=os.environ.get("AGENTS_CHAT_DATA_DIR", "./data_v2"))
    sub = parser.add_subparsers(metavar="")

    p_init = sub.add_parser("init", help="初始化 data_dir")
    p_init.set_defaults(cmd="init")

    p_worker = sub.add_parser("run-worker", help="启动 worker进程")
    p_worker.add_argument("agent_id")
    p_worker.add_argument("--cli", choices=["mock", "qwen", "opencode"])
    p_worker.set_defaults(cmd="run-worker")

    p_post = sub.add_parser("post", help="发消息到频道")
    p_post.add_argument("channel")
    p_post.add_argument("content")
    p_post.add_argument("--from", dest="from_", default="god")
    p_post.set_defaults(cmd="post")

    p_status = sub.add_parser("status", help="看 state_board")
    p_status.add_argument("task_id", nargs="?", default="")
    p_status.set_defaults(cmd="status")

    p_tail = sub.add_parser("tail", help="看频道最后 N 条消息")
    p_tail.add_argument("channel")
    p_tail.add_argument("n", type=int, nargs="?", default=10)
    p_tail.add_argument("--n", dest="n2", type=int, nargs="?", help="alias for positional N")
    p_tail.set_defaults(cmd="tail")

    p_inbox = sub.add_parser("inbox", help="看 agent 邮箱")
    p_inbox.add_argument("agent_id")
    p_inbox.set_defaults(cmd="inbox")

    p_reset = sub.add_parser("reset", help="重置 sessions/mailboxes (危险)")
    p_reset.set_defaults(cmd="reset")

    args = parser.parse_args(argv)
    if "cmd" not in args:
        parser.print_help()
        return

    if args.cmd == "init":
        cmd_init(args)
    elif args.cmd == "run-worker":
        cmd_run_worker(args)
    elif args.cmd == "post":
        cmd_post(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "tail":
        cmd_tail(args)
    elif args.cmd == "inbox":
        cmd_inbox(args)
    elif args.cmd == "reset":
        cmd_reset(args)


if __name__ == "__main__":
    main()