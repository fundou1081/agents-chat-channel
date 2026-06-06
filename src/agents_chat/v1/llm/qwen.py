"""
QwenAgent: 通过 OpenAI-compatible / Ollama 原生 API 调模型。

支持任意 OpenAI-compatible 端点:
- OpenRouter: https://openrouter.ai/api/v1 (qwen/qwen3-coder:free, 200/day)
- Ollama Cloud: https://ollama.com/v1 (qwen3-coder:480b)
- 本地 Ollama: http://localhost:11434/v1 (minimax-m2.5:cloud 等)

特别支持 minimax-m2.5 (本地 ollama daemon, 不需 OAuth):
- 用 ollama 原生 /api/chat API, 因为 OpenAI 适配层对 thinking model 不友好
- content 字段是真实输出, thinking 字段是思考过程

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
    """OpenAI-compatible / Ollama 原生 API 客户端, 输出 Decision。"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        model: str = "minimax-m2.5:cloud",
        timeout_seconds: int = 120,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        use_ollama_native: bool | None = None,  # None = auto-detect
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OLLAMA_API_KEY") or os.environ.get("QWEN_API_KEY")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        # 自动检测: localhost/127.0.0.1 用 ollama native
        if use_ollama_native is None:
            use_ollama_native = "localhost" in self.base_url or "127.0.0.1" in self.base_url
        self.use_ollama_native = use_ollama_native

        if not self.use_ollama_native and not self.api_key:
            raise RuntimeError(
                "QwenAgent 需要 API key (OpenAI-compatible 模式).\n"
                "设 OPENROUTER_API_KEY / OLLAMA_API_KEY / QWEN_API_KEY env 变量,\n"
                "或传 api_key 参数。\n"
                "免费 key: https://openrouter.ai/"
            )

    async def think(
        self,
        system: str,
        user: str,
        ctx: TickContext | None = None,
        tools: list[dict] | None = None,
    ) -> Decision:
        """调 LLM, 解析响应为 Decision。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if ctx is not None:
            messages[1]["content"] += (
                f"\n\n# 提示\n"
                f"工作目录: {ctx.persona.workdir}\n"
                f"身份: {ctx.persona.display_name} ({ctx.persona.id})\n"
                f"**重要**: 只能发邮件 (outgoing_mail), 不能改文件/跑命令。\n"
                f"output 必须严格 JSON: thinking / actions / outgoing_mail / closed_sessions / next_status\n"
            )

        try:
            if self.use_ollama_native:
                text, raw = await self._chat_ollama(messages)
            else:
                text, raw = await self._chat_openai(messages)
        except Exception as e:
            return Decision(
                thinking=f"Qwen API 失败: {e}",
                next_status="blocked",
                raw_response=str(e),
            )

        return self._parse_decision(text, ctx, raw)

    async def _chat_ollama(self, messages: list[dict]) -> tuple[str, dict]:
        """Ollama 原生 API /api/chat, 返回 (content, raw_response)。"""
        url = f"{self.base_url}/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
                data = await resp.json()
        content = data.get("message", {}).get("content", "")
        return content, data

    async def _chat_openai(self, messages: list[dict]) -> tuple[str, dict]:
        """OpenAI-compatible /chat/completions。"""
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
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
                    raise RuntimeError(f"HTTP {resp.status}: {body[:300]}")
                data = await resp.json()
        choices = data.get("choices", [])
        text = ""
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "")
            # fallback: 某些 OpenAI 适配器 (如 ollama) 把 content 放 reasoning 字段
            if not text:
                text = msg.get("reasoning", "") or msg.get("reasoning_content", "")
        return text, data

    def _parse_decision(self, text: str, ctx: TickContext | None, raw: dict | None = None) -> Decision:
        """把 LLM 输出解析成 Decision。"""
        if not text:
            return Decision(
                thinking="LLM 没输出",
                next_status="blocked",
                raw_response=str(raw)[:500] if raw else "",
            )

        json_str = _extract_json(text)
        try:
            d = json.loads(json_str)
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
                thinking=f"LLM 输出无法解析: {e}",
                outgoing_mail=outgoing,
                next_status="working" if outgoing else "idle",
                raw_response=text[:500],
            )
