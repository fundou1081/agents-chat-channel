"""Test QwenAgent with mocked HTTP. Does not need real API key."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents_chat.author.think import _extract_json
from agents_chat.llm.qwen import QwenAgent
from agents_chat.models import Persona, TickContext


class FakeResponse:
    def __init__(self, status, body_dict):
        self.status = status
        self._body = body_dict

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return json.dumps(self._body)

    async def json(self):
        return self._body


class FakeSession:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return self.response


def make_qwen_response(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


@pytest.mark.asyncio
async def test_qwen_parses_valid_json():
    """Qwen 输出合法 JSON → 解析为 Decision."""
    qwen_output = json.dumps({
        "thinking": "回复 god",
        "outgoing_mail": [
            {
                "recipients": ["god"],
                "thread_id": "t1",
                "in_reply_to": "m1",
                "subject": "Re: hi",
                "body": "hi 回来",
                "priority": 5,
                "requires_ack": False,
            }
        ],
        "closed_sessions": ["t1"],
        "next_status": "working",
    }, ensure_ascii=False)

    fake_resp = FakeResponse(200, make_qwen_response(qwen_output))
    fake_session = FakeSession(fake_resp)

    with patch("agents_chat.llm.qwen.aiohttp.ClientSession", return_value=fake_session):
        agent = QwenAgent(api_key="fake-key-for-test", model="qwen/test")
        p = Persona(id="zhang", display_name="zhang", workdir="/tmp")
        ctx = TickContext(persona=p, new_mail=[], active_sessions=[])
        decision = await agent.think(system="sys", user="user", ctx=ctx)

    assert decision.thinking == "回复 god"
    assert len(decision.outgoing_mail) == 1
    assert decision.outgoing_mail[0].recipients == ("god",)
    assert decision.outgoing_mail[0].sender == "zhang"
    assert decision.closed_sessions == ["t1"]
    assert decision.next_status == "working"


@pytest.mark.asyncio
async def test_qwen_handles_invalid_json():
    """Qwen 输出无效 JSON → fallback 成 reply body."""
    qwen_output = "我看到任务了,这就做"  # 不是 JSON

    fake_resp = FakeResponse(200, make_qwen_response(qwen_output))
    fake_session = FakeSession(fake_resp)

    with patch("agents_chat.llm.qwen.aiohttp.ClientSession", return_value=fake_session):
        agent = QwenAgent(api_key="fake", model="qwen/test")
        p = Persona(id="zhang", display_name="zhang", workdir="/tmp")
        # 模拟有一封新邮件, fallback 会回这封
        from agents_chat.models import Mail
        m = Mail.new(sender="god", recipients=["zhang"], subject="hi", body="hello")
        ctx = TickContext(persona=p, new_mail=[m], active_sessions=[])
        decision = await agent.think(system="sys", user="user", ctx=ctx)

    # fallback 路径: outgoing_mail 包含 1 封 (回 god)
    assert len(decision.outgoing_mail) == 1
    assert decision.outgoing_mail[0].recipients == ("god",)
    assert decision.outgoing_mail[0].body == qwen_output


@pytest.mark.asyncio
async def test_qwen_handles_http_error():
    """Qwen API 报错 → 返回 blocked decision."""
    fake_resp = FakeResponse(401, {"error": "unauthorized"})
    fake_session = FakeSession(fake_resp)

    with patch("agents_chat.llm.qwen.aiohttp.ClientSession", return_value=fake_session):
        agent = QwenAgent(api_key="fake", model="qwen/test")
        p = Persona(id="zhang", display_name="zhang", workdir="/tmp")
        ctx = TickContext(persona=p, new_mail=[], active_sessions=[])
        decision = await agent.think(system="sys", user="user", ctx=ctx)

    assert decision.next_status == "blocked"
    assert "401" in decision.thinking or "unauthorized" in decision.thinking.lower()


def test_qwen_requires_api_key():
    """没 API key 应该报错."""
    import os
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    saved2 = os.environ.pop("QWEN_API_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="API key"):
            QwenAgent(api_key=None)
    finally:
        if saved:
            os.environ["OPENROUTER_API_KEY"] = saved
        if saved2:
            os.environ["QWEN_API_KEY"] = saved2


def test_qwen_reads_api_key_from_env():
    """从 env 读 API key."""
    import os
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        os.environ["OPENROUTER_API_KEY"] = "from-env-key"
        agent = QwenAgent(api_key=None)
        assert agent.api_key == "from-env-key"
    finally:
        if saved:
            os.environ["OPENROUTER_API_KEY"] = saved
