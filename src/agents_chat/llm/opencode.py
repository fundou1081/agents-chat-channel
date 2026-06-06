"""
OpenCode adapter: 把 opencode run CLI 当 author 的 think backend.

opencode 是一个完整的 coding agent (类似 Claude Code):
- 自己有 agent loop
- 自动用工具 (bash, read, edit)
- 能在 workdir 里改文件
- 输出 NDJSON 事件流

我们让 author 调 opencode run, 接收它的最后 text 输出,
再解析成 Decision 格式 (邮件、关闭 session 等)。

为什么不让 opencode 跟我们一样管 session?
- 我们的 Author 抽象已经有 mailbox / heartbeat / sessions
- opencode 自己也有 session, 会冲突
- 简化: 每次 tick 调 opencode run (新 session),opencode 只负责"想 + 做"
- 我们管"消息流", opencode 管"工作流"
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import Action, Decision, Mail, TickContext
from ..author.think import _extract_json


DEFAULT_MODEL = "opencode/minimax-m3-free"


class OpenCodeAgent:
    """Author 的 think backend: 调 opencode run, 解析 JSON 输出。

    Usage:
        agent = OpenCodeAgent(model="opencode/minimax-m3-free")
        decision = await agent.think(system, user, ctx=ctx)
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        timeout_seconds: int = 120,
        extra_args: list[str] | None = None,
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        # 默认参数: --pure 跳过插件 + 跳过权限 (sandbox 限制)
        self.extra_args = extra_args or [
            "--pure",
            "--dangerously-skip-permissions",
        ]
        # 校验 opencode 可用
        if not shutil.which("opencode"):
            raise RuntimeError(
                "opencode not found in PATH. Install: brew install anomalyco/tap/opencode"
            )

    async def think(
        self,
        system: str,
        user: str,
        ctx: TickContext | None = None,
        tools: list[dict] | None = None,
    ) -> Decision:
        """调 opencode run, 解析最后输出为 Decision。"""
        prompt = self._build_prompt(system, user, ctx)
        workdir = (ctx.persona.workdir if ctx else "/tmp") or "/tmp"
        Path(workdir).mkdir(parents=True, exist_ok=True)

        events = await self._run_opencode(prompt, workdir)
        final_text = self._extract_final_text(events)
        # 收集工具调用摘要 (debug 用)
        actions_summary = self._extract_actions_summary(events)

        return self._parse_decision(final_text, ctx, actions_summary)

    # ========================================================================
    # Prompt construction
    # ========================================================================

    def _build_prompt(
        self, system: str, user: str, ctx: TickContext | None
    ) -> str:
        """构造 opencode 用的 prompt。

        包含三部分:
        1. system prompt (作者身份, 行为准则, JSON 输出格式)
        2. user context (inbox + active sessions)
        3. task (该做什么)
        """
        # opencode 是 coding agent,它的 system prompt 会被它自己处理
        # 我们要确保:
        # - 它知道自己是哪个 persona
        # - 它知道当前 inbox / sessions
        # - 它最后必须输出 JSON

        # 把 system + user 拼起来
        parts = []
        parts.append(system)
        if user:
            parts.append("\n# Context\n" + user)
        if ctx is not None:
            # 补充信息: tool usage hint
            parts.append(
                "\n# 你的工作目录\n"
                f"你可以在 {ctx.persona.workdir} 目录下用 bash 跑命令、读/写文件。\n"
                f"你的身份: {ctx.persona.display_name} ({ctx.persona.id})\n"
            )
        parts.append(
            "\n# 重要: 输出格式\n"
            "完成上面的 inbox 处理后, **最后必须输出一个 JSON 决策对象** (纯 JSON, 无 markdown 包裹):\n"
            "```\n"
            "{\n"
            '  "thinking": "<你刚做了什么, 一句话>",\n'
            '  "actions": [{"type":"use_tool","payload":{"tool":"...","input":"..."}}, ...],\n'
            '  "outgoing_mail": [\n'
            '    {\n'
            '      "recipients": ["<收件人 id>"],\n'
            '      "thread_id": "<原 thread_id>",\n'
            '      "in_reply_to": "<原 mail_id>",\n'
            '      "subject": "Re: <原 subject>",\n'
            '      "body": "<你的回复, 中文, 多行用 \\n>",\n'
            '      "priority": 5,\n'
            '      "requires_ack": false\n'
            '    }\n'
            '  ],\n'
            '  "closed_sessions": ["<thread_id>", ...],\n'
            '  "next_status": "idle" | "working" | "blocked"\n'
            "}\n"
            "```\n"
        )
        return "\n".join(parts)

    # ========================================================================
    # Subprocess
    # ========================================================================

    async def _run_opencode(self, prompt: str, workdir: str) -> list[dict]:
        """调 opencode run, 返回 NDJSON events。"""
        args = [
            "opencode", "run",
            "--format", "json",
            "--model", self.model,
            "--dir", workdir,
            *self.extra_args,
            prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
            )
            events: list[dict] = []
            last_event_at = asyncio.get_event_loop().time()
            
            async def read_stdout():
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        return
                    line_str = line.decode(errors="replace").strip()
                    if not line_str:
                        continue
                    try:
                        e = json.loads(line_str)
                        events.append(e)
                    except json.JSONDecodeError:
                        pass

            try:
                await asyncio.wait_for(read_stdout(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                events.append({"type": "error", "error": "timeout"})

            stderr_data = await proc.stderr.read() if proc.stderr else b""
            await proc.wait()
            
            if stderr_data:
                err = stderr_data.decode(errors="replace").strip()
                if err:
                    print(f"  [opencode-debug] stderr: {err[:200]}")
            
            print(f"  [opencode-debug] got {len(events)} events, types: {[e.get('type', '?') for e in events]}")
            return events
        except FileNotFoundError:
            return [{"type": "error", "error": "opencode not found"}]
        except Exception as e:
            return [{"type": "error", "error": str(e)}]

    # ========================================================================
    # Output parsing
    # ========================================================================

    def _extract_final_text(self, events: list[dict]) -> str:
        """提取最后一个 text 事件的内容 (opencode 最后输出应该是决策 JSON)。"""
        texts = []
        for e in events:
            if e.get("type") == "text":
                t = e.get("part", {}).get("text", "")
                if t:
                    texts.append(t)
        result = "\n".join(texts) if texts else ""
        if not result and events:
            # debug: 打印所有 event types
            types = [e.get("type", "?") for e in events[-10:]]
            print(f"  [opencode-debug] no text events. last 10 types: {types}")
        return result

    def _extract_actions_summary(self, events: list[dict]) -> list[dict]:
        """从事件流提取 tool 调用摘要, 作为 author 的 action 记录。"""
        actions = []
        for e in events:
            if e.get("type") == "tool_use":
                part = e.get("part", {})
                tool = part.get("tool", "unknown")
                state = part.get("state", {})
                inp = state.get("input", {})
                out = state.get("output", "")
                actions.append({
                    "tool": tool,
                    "input": inp,
                    "output_preview": (out or "")[:200],
                })
        return actions

    def _parse_decision(
        self,
        text: str,
        ctx: TickContext | None,
        actions_summary: list[dict],
    ) -> Decision:
        """把 opencode 的 text 输出解析成 Decision。"""
        if not text:
            return Decision(
                thinking="opencode 没输出 (可能超时或失败)",
                next_status="blocked",
            )

        # 尝试从 text 中抽取 JSON
        json_str = _extract_json(text)
        try:
            d = json.loads(json_str)
            # 修复 sender / id / created_at 占位
            persona_id = ctx.persona.id if ctx else "agent"
            for m in d.get("outgoing_mail", []):
                m.setdefault("id", str(uuid.uuid4())[:12])
                m["sender"] = persona_id  # 强制是 author 自己
                m.setdefault("created_at", datetime.now().isoformat())
                m.setdefault("priority", 5)
                m.setdefault("requires_ack", False)
                m.setdefault("in_reply_to", None)
                m.setdefault("thread_id", str(uuid.uuid4())[:8])
                m.setdefault("subject", "")
                m.setdefault("body", "")
                m.setdefault("recipients", [])
                m.setdefault("attachments", [])
                m.setdefault("metadata", {})

            decision = Decision.from_dict(d)
            # 把 opencode 真实做的 tool 调用也加到 actions
            for a in actions_summary:
                decision.actions.append(Action(
                    type="use_tool",
                    payload={"tool": a["tool"], "input": a["input"]},
                ))
            return decision
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            # fallback: 把整个 text 当作 reply body 发出去
            thinking_msg = f"opencode 输出无法解析为 JSON, 当成 text 处理"
            outgoing = []
            if ctx and ctx.new_mail:
                # 找第一个邮件,回给它
                m = ctx.new_mail[0]
                outgoing.append(Mail(
                    id=str(uuid.uuid4())[:12],
                    sender=ctx.persona.id,
                    recipients=(m.sender,),
                    thread_id=m.thread_id,
                    in_reply_to=m.id,
                    subject=f"Re: {m.subject}" if m.subject else "",
                    body=text[:2000],
                    priority=5,
                    created_at=datetime.now(),
                ))
            # 工具调用也记录
            actions = [
                Action(type="use_tool", payload=a) for a in actions_summary
            ]
            return Decision(
                thinking=f"{thinking_msg}: {e}",
                actions=actions,
                outgoing_mail=outgoing,
                next_status="working" if outgoing or actions else "idle",
            )
