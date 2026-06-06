"""
QwenCLI for v2.0 — HTTP API 适配 (qwen 不支持原生 resume, 用本地 history 模拟).

qwen 是 OpenAI-compatible HTTP API, 没有 --resume 命令.
本实现:
  - 首次调用: 调 qwen API, 返回 new_session_id (本地生成)
  - resume: 把 history 拼成 messages[], 整个发给 qwen (qwen 自己不记忆, history 在本地)

适用场景: qwen / OpenRouter / Ollama / 其他 OpenAI-compatible API.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp

from .base import CLIResponse, new_session_id


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "qwen/qwen3-coder:free"


class QwenCLI:
    name: str = "qwen"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        timeout_seconds: int = 120,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        history_dir: str | Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("QWEN_API_KEY")
        self.timeout = timeout_seconds
        self.temperature = temperature
        self.max_tokens = max_tokens
        # history_dir: 存每个 session 的 history (模拟 resume)
        self.history_dir = Path(history_dir) if history_dir else None
        if self.history_dir:
            self.history_dir.mkdir(parents=True, exist_ok=True)
        self.call_count = 0

    def _history_path(self, session_id: str) -> Path:
        assert self.history_dir, "history_dir not set"
        return self.history_dir / f"{session_id}.json"

    def _load_history(self, session_id: str) -> list[dict]:
        p = self._history_path(session_id)
        if p.exists():
            try:
                return json.loads(p.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_history(self, session_id: str, history: list[dict]):
        p = self._history_path(session_id)
        p.write_text(json.dumps(history, ensure_ascii=False, indent=2))

    async def invoke(
        self, prompt: str, resume_session: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> CLIResponse:
        start = time.time()
        self.call_count += 1

        # workspace_dir 对 HTTP API 不直接生效 (qwen 在远程), 但可以在 prompt 里提示
        # Agent 写了 {workspace_dir}/qwen.md (引导文件), 这里把"看 qwen.md"注入 prompt
        if workspace_dir:
            md_path = Path(workspace_dir) / "qwen.md"
            if md_path.exists():
                md_content = md_path.read_text("utf-8")[:2000]  # 限 2K 防爆
                prompt = (
                    f"[Workspace Guide: read {md_path}]\n{md_content}\n\n"
                    f"[Task]\n{prompt}"
                )

        if not self.api_key:
            return CLIResponse(
                output_text="",
                error="QwenCLI: api_key not set (env OPENROUTER_API_KEY or QWEN_API_KEY)",
                elapsed_ms=int((time.time() - start) * 1000),
            )

        # 决定 session id
        if resume_session:
            session_id = resume_session
        else:
            session_id = new_session_id("qwen")

        # 构造 messages (含 history 模拟 resume)
        messages: list[dict] = []
        if self.history_dir and resume_session:
            for h in self._load_history(session_id):
                messages.append(h)
        messages.append({"role": "user", "content": prompt})

        # 调 OpenAI-compatible API
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=body) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        return CLIResponse(
                            output_text="",
                            error=f"qwen API {resp.status}: {err_text[:500]}",
                            elapsed_ms=int((time.time() - start) * 1000),
                        )
                    data = await resp.json()

            # 提取 reply
            choice = data.get("choices", [{}])[0]
            reply = choice.get("message", {}).get("content", "").strip()

            # 保存 history (用于下次 resume)
            if self.history_dir:
                history = self._load_history(session_id) if resume_session else []
                history.append({"role": "user", "content": prompt})
                history.append({"role": "assistant", "content": reply})
                self._save_history(session_id, history)

            new_id = None if resume_session else session_id
            return CLIResponse(
                output_text=reply,
                new_session_id=new_id,
                raw=reply,
                elapsed_ms=int((time.time() - start) * 1000),
            )
        except asyncio.TimeoutError:
            return CLIResponse(
                output_text="",
                error=f"qwen API timeout after {self.timeout}s",
                elapsed_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return CLIResponse(
                output_text="",
                error=f"qwen API error: {e}",
                elapsed_ms=int((time.time() - start) * 1000),
            )
