"""
A2AClient — A2A 协议 CLI adapter (我们 worker 调外部 A2A server).

设计: 跟 OpenCodeCLI / QwenCLI 同一层, 业务代码 (DecisionMaker / EventHandler) 不感知.
       通过 config.json 启用: {"cli": "a2a", "cli_config": {"a2a_url": "https://..."}}

跟 EventBus + busd 配合:
  - A2AClient.execute() 调外部 HTTP 端点, 跨网络 (~100-500ms)
  - 跟我们"共享频道"模式正交, 不破坏多 agent 协调
  - 配合 watchdog + busd, 整体感知延迟仍 < 50ms

协议参考: docs/23-a2a-research.md (Phase B 阶段 2)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from .base import CLI, CLIResponse, new_session_id

logger = logging.getLogger("a2a-client")


class A2AClient(CLI):
    """A2A 协议 client. 跟 OpenCodeCLI / QwenCLI 同一抽象.

    Usage:
        client = A2AClient(
            agent_url="https://external-bargain-agent.com",
            api_key="secret",  # 可选
            timeout=30.0,
        )
        result = await client.execute(prompt="开价")
        print(result.output_text)  # "100 元一斤"
    """

    name = "a2a"

    def __init__(
        self,
        agent_url: str,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        workspace_dir: Optional[str | Path] = None,
    ) -> None:
        self.agent_url = agent_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.workspace_dir = Path(workspace_dir) if workspace_dir else None
        self._card: Optional[dict] = None  # 缓存 Agent Card
        self._card_lock = asyncio.Lock()
        # 持久 session_id (跟 OpenCodeCLI 一致: 每个 worker 一个 session)
        self.session_id = new_session_id("a2a")

    async def execute(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        workspace_dir: Optional[str | Path] = None,
    ) -> CLIResponse:
        """调外部 A2A agent, 拿 reply 返 CLIResponse.

        session_id: 我们 worker 传 (DecisionMaker 给的), 用于 A2A server 端 stateful task
                    (默认: 用 self.session_id)
        workspace_dir: A2A client 不直接用, 但跟其他 CLI 保持一致接口
        """
        # 1. 第一次调: 拉 Agent Card (缓存)
        await self._ensure_card()

        # 2. 构造 A2A message/send 请求
        a2a_session = session_id or self.session_id
        task = {
            "id": a2a_session,
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": prompt}],
            },
            "metadata": {
                # 告诉 server 我们是哪个 worker (server 端可选路由)
                "agent_id": self.agent_url.split("/")[-1] or "a2a-client",
            },
        }

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # 3. HTTP POST /v1/message/send
        url = f"{self.agent_url}/v1/message/send"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(url, json=task, headers=headers)
                r.raise_for_status()
                result = r.json()
        except httpx.TimeoutException as e:
            return CLIResponse(
                output_text="",
                error=f"A2AClient timeout: {self.agent_url} > {self.timeout}s",
                new_session_id=a2a_session,
                raw=f"TimeoutException: {e}",
            )
        except httpx.HTTPStatusError as e:
            return CLIResponse(
                output_text="",
                error=f"A2AClient HTTP {e.response.status_code}: {e.response.text[:200]}",
                new_session_id=a2a_session,
                raw=f"HTTPStatusError: {e}",
            )
        except httpx.RequestError as e:
            return CLIResponse(
                output_text="",
                error=f"A2AClient connection error: {e}",
                new_session_id=a2a_session,
                raw=f"RequestError: {e}",
            )

        # 4. 解析 A2A Task → CLIResponse
        output_text = self._extract_text(result)
        a2a_status = result.get("status", "unknown")
        error = "" if a2a_status == "completed" else f"A2A status={a2a_status}"

        # 5. 返 CLIResponse (跟 OpenCodeCLI 一致)
        return CLIResponse(
            output_text=output_text,
            new_session_id=a2a_session,
            error=error,
            raw=json.dumps(result),  # raw 存 A2A Task 完整 JSON
        )

    async def _ensure_card(self) -> None:
        """拉 Agent Card 并缓存."""
        if self._card is not None:
            return
        async with self._card_lock:
            if self._card is not None:
                return
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{self.agent_url}/.well-known/agent.json")
                    r.raise_for_status()
                    self._card = r.json()
                logger.info(
                    f"A2AClient: loaded card from {self.agent_url} - "
                    f"name={self._card.get('name')}, skills={len(self._card.get('skills', []))}"
                )
            except Exception as e:
                # 拿不到 card 不致命 (server 可能不返 .well-known/agent.json)
                logger.warning(f"A2AClient: failed to load card from {self.agent_url}: {e}")
                self._card = {}

    def _extract_text(self, task: dict) -> str:
        """从 A2A Task 提取 text artifact.

        A2A Task 结构:
          { "artifacts": [{"name": "...", "parts": [{"type": "text", "text": "..."}]}] }
        """
        parts: list[str] = []
        for artifact in task.get("artifacts", []):
            for part in artifact.get("parts", []):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
        return "\n".join(parts).strip() or f"[A2AClient no-text-reply] status={task.get('status')}"

    def get_card(self) -> Optional[dict]:
        """返缓存的 Agent Card (调试用, 可能 None)."""
        return self._card

    def __repr__(self) -> str:
        return f"<A2AClient url={self.agent_url} session={self.session_id[:20]}>"
