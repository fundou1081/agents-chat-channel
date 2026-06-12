"""
A2AClient 单元测试.

覆盖:
- 基础 execute: mock A2A server, 验 reply 解析
- Agent Card 拉取 + 缓存
- 鉴权 (Bearer token)
- 错误处理 (timeout / HTTP error / connection)
- Session 透传
- worker_factory 集成 ("a2a" cli_type)
"""
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from agents_chat.infra.cli.a2a import A2AClient
from agents_chat.infra.cli.base import CLIResponse
from agents_chat.infra.worker_factory import WorkerFactory, register_cli, list_clis


# =============================================================================
# 基础构造 + 字段
# =============================================================================


class TestA2AClientBasics:
    def test_construct(self):
        client = A2AClient(agent_url="https://example.com/a2a")
        assert client.name == "a2a"
        assert client.agent_url == "https://example.com/a2a"
        assert client.api_key is None
        assert client.timeout == 30.0
        assert client.session_id.startswith("a2a_")

    def test_construct_with_api_key(self):
        client = A2AClient(
            agent_url="https://example.com/",
            api_key="secret-key",
            timeout=60.0,
        )
        assert client.api_key == "secret-key"
        assert client.timeout == 60.0
        assert client.agent_url == "https://example.com"  # 末尾 / 去掉

    def test_repr(self):
        client = A2AClient(agent_url="https://example.com/agent")
        r = repr(client)
        assert "A2AClient" in r
        assert "https://example.com/agent" in r


# =============================================================================
# Agent Card 拉取 + 缓存
# =============================================================================


class TestAgentCard:
    @pytest.mark.asyncio
    async def test_fetch_card(self):
        client = A2AClient(agent_url="https://example.com")

        # Mock httpx.get
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "name": "TestAgent",
            "version": "1.0.0",
            "skills": [{"id": "test", "name": "Test"}],
        }
        mock_response.raise_for_status = MagicMock()

        # 用 httpx.MockTransport 拦截所有 HTTP 请求
        def handler(request):
            return httpx.Response(200, json={
                "name": "TestAgent",
                "version": "1.0.0",
                "skills": [{"id": "test", "name": "Test"}],
            })
        transport = httpx.MockTransport(handler)
        with patch("httpx.AsyncClient") as MockClient:
            instance = MagicMock()
            instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            await client._ensure_card()
        
        assert client._card["name"] == "TestAgent"

    @pytest.mark.asyncio
    async def test_card_cached(self):
        """第二次调用不重新发 HTTP."""
        client = A2AClient(agent_url="https://example.com")
        # 预填 cache
        client._card = {"name": "Cached"}

        # cache 预填, 不应该再调 get
        # 用 MagicMock 验 instance.get 没被调
        instance = MagicMock()
        with patch("httpx.AsyncClient") as MockClient:
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            await client._ensure_card()
        instance.get.assert_not_called()
        assert client._card["name"] == "Cached"

    @pytest.mark.asyncio
    async def test_card_load_failure_silent(self):
        """拉 card 失败不抛错, 用空 card (server 可能不返 card)."""
        client = A2AClient(agent_url="https://example.com")
        with patch("httpx.AsyncClient") as MockClient:
            instance = MagicMock()
            instance.get = AsyncMock(side_effect=httpx.RequestError("conn err"))
            MockClient.return_value.__aenter__ = AsyncMock(return_value=instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=None)
            await client._ensure_card()
        assert client._card == {}  # 空 card, 不抛


# =============================================================================
# execute() — 主要功能
# =============================================================================


def _mock_task_response(
    text: str = "Hello from A2A agent",
    task_id: str = "t_001",
    status: str = "completed",
) -> dict:
    """构造 mock A2A Task response."""
    return {
        "id": task_id,
        "status": status,
        "artifacts": [
            {
                "name": "reply",
                "parts": [{"type": "text", "text": text}],
            }
        ],
    }


class TestA2AClientExecute:
    @pytest.mark.asyncio
    async def test_basic_execute(self):
        client = A2AClient(agent_url="https://example.com")
        client._card = {"name": "TestAgent"}  # 跳过 card 拉取

        mock_response = MagicMock()
        mock_response.json.return_value = _mock_task_response("Hello A2A")
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_response)):
            result = await client.execute(prompt="hi")

        assert isinstance(result, CLIResponse)
        assert result.output_text == "Hello A2A"
        assert result.error == "" and "Hello A2A" in result.output_text

    @pytest.mark.asyncio
    async def test_execute_sends_correct_a2a_message(self):
        """验证发给 server 的 JSON 结构符合 A2A 规范."""
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        captured = {}
        async def capture_post(url, **kwargs):
            captured["json"] = kwargs.get("json")
            captured["url"] = url
            mock_response = MagicMock()
            mock_response.json.return_value = _mock_task_response("ok")
            mock_response.raise_for_status = MagicMock()
            return mock_response
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture_post)):
            await client.execute(prompt="hello world")

        # 验证 URL
        assert captured["url"] == "https://example.com/v1/message/send"
        # 验证 body 结构
        body = captured["json"]
        assert body["message"]["role"] == "user"
        assert body["message"]["parts"][0]["type"] == "text"
        assert body["message"]["parts"][0]["text"] == "hello world"
        assert "id" in body
        assert "metadata" in body

    @pytest.mark.asyncio
    async def test_execute_with_session_id(self):
        """worker 传 session_id 应该透传 (stateful server 端)."""
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        captured = {}
        async def capture(url, **kwargs):
            captured["body"] = kwargs.get("json")
            r = MagicMock()
            r.json.return_value = _mock_task_response()
            r.raise_for_status = MagicMock()
            return r
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture)):
            await client.execute(prompt="x", session_id="my-session-42")

        assert captured["body"]["id"] == "my-session-42"

    @pytest.mark.asyncio
    async def test_execute_with_api_key_sends_auth(self):
        client = A2AClient(agent_url="https://example.com", api_key="secret-123")
        client._card = {}

        captured = {}
        async def capture(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            r = MagicMock()
            r.json.return_value = _mock_task_response()
            r.raise_for_status = MagicMock()
            return r
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture)):
            await client.execute(prompt="x")

        assert captured["headers"]["Authorization"] == "Bearer secret-123"

    @pytest.mark.asyncio
    async def test_execute_no_api_key_no_auth_header(self):
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        captured = {}
        async def capture(url, **kwargs):
            captured["headers"] = kwargs.get("headers", {})
            r = MagicMock()
            r.json.return_value = _mock_task_response()
            r.raise_for_status = MagicMock()
            return r
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture)):
            await client.execute(prompt="x")

        assert "Authorization" not in captured["headers"]


# =============================================================================
# execute() — 错误处理
# =============================================================================


class TestA2AClientErrors:
    @pytest.mark.asyncio
    async def test_timeout(self):
        client = A2AClient(agent_url="https://example.com", timeout=1.0)
        client._card = {}

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.TimeoutException("timeout"))):
            result = await client.execute(prompt="x")

        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    async def test_http_error(self):
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal server error"
        # raise_for_status 抛 HTTPStatusError
        def raise_():
            raise httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
        mock_response.raise_for_status = raise_

        async def capture(url, **kwargs):
            return mock_response
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture)):
            result = await client.execute(prompt="x")

        assert "500" in result.error

    @pytest.mark.asyncio
    async def test_connection_error(self):
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            result = await client.execute(prompt="x")

        assert "connection" in result.error.lower() or "refused" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_text_in_response(self):
        """server 返 reply 但没 text part."""
        client = A2AClient(agent_url="https://example.com")
        client._card = {}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "t_001",
            "status": "completed",
            "artifacts": [],  # 空
        }
        mock_response.raise_for_status = MagicMock()

        async def capture(url, **kwargs):
            return mock_response
        with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=capture)):
            result = await client.execute(prompt="x")

        # 不抛错, 给个提示
        # 返 ok (error=="") 或 no-text-reply 提示
        assert "no-text-reply" in result.output_text or result.error == ""


# =============================================================================
# _extract_text
# =============================================================================


class TestExtractText:
    def test_single_text_artifact(self):
        client = A2AClient(agent_url="https://x")
        task = _mock_task_response("hello")
        assert client._extract_text(task) == "hello"

    def test_multiple_text_artifacts(self):
        client = A2AClient(agent_url="https://x")
        task = {
            "artifacts": [
                {"parts": [{"type": "text", "text": "first"}]},
                {"parts": [{"type": "text", "text": "second"}]},
            ]
        }
        assert client._extract_text(task) == "first\nsecond"

    def test_non_text_parts_skipped(self):
        client = A2AClient(agent_url="https://x")
        task = {
            "artifacts": [
                {"parts": [
                    {"type": "file", "file_url": "https://x/y.png"},
                    {"type": "text", "text": "desc"},
                ]}
            ]
        }
        assert client._extract_text(task) == "desc"

    def test_empty_artifacts(self):
        client = A2AClient(agent_url="https://x")
        assert "no-text-reply" in client._extract_text({"status": "completed"})


# =============================================================================
# worker_factory 集成
# =============================================================================


class TestWorkerFactoryIntegration:
    def test_a2a_registered(self):
        assert "a2a" in list_clis()

    def test_create_a2a_worker(self, tmp_path):
        from pathlib import Path
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        (data_dir / "workspaces").mkdir()

        worker = WorkerFactory.create(
            agent_id="seller-fish",
            cli_type="a2a",
            data_dir=data_dir,
            cli_config={"a2a_url": "https://external.example.com/a2a"},
        )
        # 验证 worker 创建成功, cli 是 A2AClient
        assert worker.cli.name == "a2a"
        assert worker.cli.agent_url == "https://external.example.com/a2a"

    def test_create_a2a_worker_missing_url(self, tmp_path):
        """cli_config 没有 a2a_url 应该 raise."""
        from pathlib import Path
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        with pytest.raises(ValueError, match="a2a_url"):
            WorkerFactory.create(
                agent_id="seller-fish",
                cli_type="a2a",
                data_dir=data_dir,
                cli_config={},  # 没 a2a_url
            )

    def test_create_a2a_worker_with_api_key(self, tmp_path):
        from pathlib import Path
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        worker = WorkerFactory.create(
            agent_id="seller-fish",
            cli_type="a2a",
            data_dir=data_dir,
            cli_config={
                "a2a_url": "https://external.example.com",
                "a2a_api_key": "secret",
            },
        )
        assert worker.cli.api_key == "secret"
