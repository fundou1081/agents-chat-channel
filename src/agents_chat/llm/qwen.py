"""
QwenAgent: 直接调 OpenAI-compatible HTTP API (走 OpenRouter 免费 qwen3-coder).

为什么不用 qwen CLI?
- Qwen Code CLI 的 OAuth free tier 2026-04-15 停服
- 现在用 qwen CLI 必须 Alibaba Cloud Coding Plan (付费) 或自配 provider
- 我们直接调 OpenRouter / Ollama Cloud 的 OpenAI-compatible API, 更直接

支持:
- OpenRouter (qwen/qwen3-coder:free, 200 req/day 免费)
- Ollama Cloud (qwen3-coder:480b, ollama cloud 订阅)
- 其他任何 OpenAI-compatible 端点

接口跟 OpenCodeAgent / MockLLM 一样:
  async def think(system, user, ctx=None, tools=None) -> Decision
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime
from typing import Any

import aiohttp

from ..models import Action, Decision, Mail, TickContext
from ..author.think import _extract_json


class QwenAgent:
    """通过 OpenAI-compatible HTTP API 调 Qwen (走 OpenRouter 或 Ollama Cloud)。"""

    def __init__(
        self,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str | None = None,
        model: str = "qwen/qwen3-coder:free",
        timeout_seconds: int = 120,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QWEN_API_KEY")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        if not self.api_key:
            raise RuntimeError(
                "QwenAgent 需要 API key. 设置 OPENROUTER_API_KEY env 或传入 api_key 参数.\n"
                "免费 key: https://openrouter.ai/ (注册后 Settings → Keys)"
            )

    async def think(
        self,
        system: str,
        user: str,
        ctx: TickContext | None = None,
        tools: list[dict] | None = None,
    ) -> Decision:
        """调 chat/completions API, 解析响应为 Decision。

        注意: Qwen3-Coder 是纯 LLM, 不会调工具 (没有 agent loop).
        它只能输出文本. 我们强制 prompt 让它输出 JSON.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if ctx is not None:
            # 加 workdir hint
            messages[1]["content"] += (
                f"\n\n# 提示\n"
                f"你的工作目录: {ctx.persona.workdir}\n"
                f"你是 {ctx.persona.display_name} ({ctx.persona.id})\n"
                f"**重要**: 你只能发邮件 (outgoing_mail), 不能直接改文件或跑命令.\n"
                f"output 必须严格 JSON 格式, 字段: thinking / actions / outgoing_mail / closed_sessions / next_status\n"
            )

        try:
            text, usage = await self._chat(messages)
        except Exception as e:
            return Decision(
                thinking=f"Qwen API 调用失败: {e}",
                next_status="blocked",
            )

        return self._parse_decision(text, ctx)

    async def _chat(self, messages: list[dict]) -> tuple[str, dict]:
        """调 OpenAI-compatible /chat/completions."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {body[:500]}")
                data = await resp.json()

        # OpenAI 格式: choices[0].message.content
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"no choices: {data}")
        text = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        return text, usage

    def _parse_decision(self, text: str, ctx: TickContext | None) -> Decision:
        """把 LLM 输出解析成 Decision。"""
        if not text:
            return Decision(
                thinking="Qwen 没输出",
                next_status="blocked",
            )

        json_str = _extract_json(text)
        try:
            d = json.loads(json_str)
            # 修复 outgoing_mail
            persona_id = ctx.persona.id if ctx else "agent"
            for m in d.get("outgoing_mail", []):
                m.setdefault("id", str(uuid.uuid4())[:12])
                m["sender"] = persona_id
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

            return Decision.from_dict(d)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            # fallback: 把 text 当 reply body
            outgoing = []
            if ctx and ctx.new_mail:
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
            return Decision(
                thinking=f"Qwen 输出无法解析为 JSON: {e}",
                outgoing_mail=outgoing,
                next_status="working" if outgoing else "idle",
            )
