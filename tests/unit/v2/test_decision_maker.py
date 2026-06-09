"""Tests for v2.0 DecisionMaker (LLM 决定 session 续/新建/skip).

覆盖:
  - DecisionConfig: 默认值, env 变量
  - Decision: 数据类 + to_dict
  - DecisionMaker.decide (mock client):
    - continue 正常
    - new 正常
    - skip 正常
    - 邮箱路径下 skip → 强制 new
    - continue + 无效 session_id → 强制 new
    - LLM 输出无法解析 → fallback (邮箱→new, 轮询→skip)
    - 非法 action → fallback
    - LLM 异常 → 抛 (EventHandler fallback)
  - EventHandler 集成:
    - DecisionMaker 决定 continue → 调 CLI
    - DecisionMaker 决定 new → 调 CLI + 新建 session
    - DecisionMaker 决定 skip → 写 system 消息, 不调 CLI
    - DecisionMaker 失败 → fallback SessionManager.decide_session
    - 邮箱路径下 skip 强制 new

目标: ≥25 tests, 全部过.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock

from agents_chat.v2.core.decision import (
    Decision,
    DecisionConfig,
    DecisionMaker,
)


# =============================================================================
# Mock client
# =============================================================================


class MockLLMClient:
    """Mock LLM 客户端, 可预设返回值."""

    def __init__(self, response: str = "", raise_exc: Exception | None = None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    async def chat(self, *, model, messages, temperature, timeout) -> str:
        self.calls.append({
            "model": model, "messages": messages,
            "temperature": temperature, "timeout": timeout,
        })
        if self.raise_exc:
            raise self.raise_exc
        return self.response


# =============================================================================
# DecisionConfig
# =============================================================================


class TestDecisionConfig:
    def test_defaults(self):
        cfg = DecisionConfig()
        assert cfg.temperature == 0.0
        assert cfg.timeout == 10.0
        assert cfg.max_retries == 1
        assert cfg.model  # 有默认

    def test_is_valid_without_api_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DECISION_API_KEY", raising=False)
        cfg = DecisionConfig()
        # 默认 api_key 是 "" → invalid
        assert cfg.is_valid() is False

    def test_is_valid_with_api_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "test-key-1234567890")
        cfg = DecisionConfig()
        assert cfg.is_valid() is True

    def test_env_override_model(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "test")
        monkeypatch.setenv("DECISION_MODEL", "custom-model")
        cfg = DecisionConfig()
        assert cfg.model == "custom-model"


# =============================================================================
# Decision 数据类
# =============================================================================


class TestDecision:
    def test_continue(self):
        d = Decision(action="continue", session_id="s1", reason="续")
        assert d.action == "continue"
        assert d.session_id == "s1"

    def test_new(self):
        d = Decision(action="new", reason="新话题")
        assert d.action == "new"

    def test_skip(self):
        d = Decision(action="skip", reason="无关")
        assert d.action == "skip"

    def test_to_dict(self):
        d = Decision(action="continue", session_id="s1", reason="r", raw="raw")
        dd = d.to_dict()
        assert dd["action"] == "continue"
        assert dd["session_id"] == "s1"
        assert dd["raw"] == "raw"


# =============================================================================
# DecisionMaker.decide (mock client)
# =============================================================================


class TestDecisionMakerDecide:
    @pytest.fixture
    def cfg(self):
        return DecisionConfig(base_url="http://test", api_key="k", model="m")

    @pytest.mark.asyncio
    async def test_continue_正常(self, cfg):
        client = MockLLMClient(response='{"action": "continue", "session_id": "s1", "reason": "r"}')
        dm = DecisionMaker(cfg, client=client)
        sessions = [{"session_id": "s1", "topic": "t", "progress": 50}]
        d = await dm.decide(
            mail={"content": "继续聊", "path": "email"},
            sessions=sessions, role="你是 bot", is_must_reply=True,
        )
        assert d.action == "continue"
        assert d.session_id == "s1"
        # 确认 LLM call
        assert len(client.calls) == 1
        assert client.calls[0]["model"] == "m"
        assert client.calls[0]["temperature"] == 0.0

    @pytest.mark.asyncio
    async def test_new_正常(self, cfg):
        client = MockLLMClient(response='{"action": "new", "reason": "新话题"}')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "完全不同", "path": "email"},
            sessions=[], role="bot", is_must_reply=True,
        )
        assert d.action == "new"

    @pytest.mark.asyncio
    async def test_skip_轮询路径(self, cfg):
        """轮询路径 (is_must_reply=False) LLM 决定 skip."""
        client = MockLLMClient(response='{"action": "skip", "reason": "不相关"}')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "无关消息", "path": "poll"},
            sessions=[], role="bot", is_must_reply=False,
        )
        assert d.action == "skip"
        assert d.reason == "不相关"

    @pytest.mark.asyncio
    async def test_邮箱路径_skip_强制_new(self, cfg):
        """邮箱路径 (is_must_reply=True) LLM 想 skip → 强制改 new."""
        client = MockLLMClient(response='{"action": "skip", "reason": "LLM 想跳过"}')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "@bot hi", "path": "email"},
            sessions=[], role="bot", is_must_reply=True,
        )
        assert d.action == "new"  # ← 强制改写
        assert "强制必答" in d.reason
        assert "LLM 想跳过" in d.reason

    @pytest.mark.asyncio
    async def test_continue_无效_session_id_强制_new(self, cfg):
        """continue 但 session_id 不在 sessions 列表 → 强制 new."""
        client = MockLLMClient(response='{"action": "continue", "session_id": "s_ghost", "reason": "r"}')
        dm = DecisionMaker(cfg, client=client)
        sessions = [{"session_id": "s1", "topic": "t"}]
        d = await dm.decide(
            mail={"content": "x", "path": "email"},
            sessions=sessions, role="bot", is_must_reply=True,
        )
        assert d.action == "new"
        assert "session_id 无效" in d.reason

    @pytest.mark.asyncio
    async def test_continue_无_session_id_字段_强制_new(self, cfg):
        """continue 但没填 session_id → 强制 new."""
        client = MockLLMClient(response='{"action": "continue", "reason": "r"}')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "x", "path": "email"},
            sessions=[{"session_id": "s1"}], role="bot", is_must_reply=True,
        )
        assert d.action == "new"

    @pytest.mark.asyncio
    async def test_LLM_输出_无法解析_邮箱_fallback_new(self, cfg):
        """LLM 输出垃圾 → 邮箱路径 fallback new."""
        client = MockLLMClient(response="not even json")
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "x", "path": "email"},
            sessions=[], role="bot", is_must_reply=True,
        )
        assert d.action == "new"
        assert "无法解析" in d.reason

    @pytest.mark.asyncio
    async def test_LLM_输出_无法解析_轮询_fallback_skip(self, cfg):
        """LLM 输出垃圾 → 轮询路径 fallback skip."""
        client = MockLLMClient(response="garbage")
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "x", "path": "poll"},
            sessions=[], role="bot", is_must_reply=False,
        )
        assert d.action == "skip"

    @pytest.mark.asyncio
    async def test_非法_action_强制_new(self, cfg):
        """action 是 'maybe' (非法) → 强制 new."""
        client = MockLLMClient(response='{"action": "maybe", "reason": "r"}')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "x", "path": "email"},
            sessions=[], role="bot", is_must_reply=True,
        )
        assert d.action == "new"
        assert "非法" in d.reason

    @pytest.mark.asyncio
    async def test_LLM_异常_抛出让_EventHandler_fallback(self, cfg):
        """LLM 抛异常 → DecisionMaker.decide 抛出, 不静默吞."""
        client = MockLLMClient(raise_exc=RuntimeError("LLM down"))
        dm = DecisionMaker(cfg, client=client)
        with pytest.raises(RuntimeError, match="LLM down"):
            await dm.decide(
                mail={"content": "x", "path": "email"},
                sessions=[], role="bot", is_must_reply=True,
            )

    @pytest.mark.asyncio
    async def test_LLM_输出_嵌在文本里_正则提取(self, cfg):
        """LLM 输出 'blabla {"action": ...} blabla' → 提取 JSON."""
        client = MockLLMClient(response='一些废话\n{"action": "skip", "reason": "不相关"}\n更多废话')
        dm = DecisionMaker(cfg, client=client)
        d = await dm.decide(
            mail={"content": "x", "path": "poll"},
            sessions=[], role="bot", is_must_reply=False,
        )
        assert d.action == "skip"

    @pytest.mark.asyncio
    async def test_prompt_含_role_and_sessions(self, cfg):
        """确认 prompt 里包含 role 和 sessions."""
        client = MockLLMClient(response='{"action": "new"}')
        dm = DecisionMaker(cfg, client=client)
        await dm.decide(
            mail={"content": "test mail", "channel": "general", "path": "email"},
            sessions=[{"session_id": "s1", "topic": "买鱼", "progress": 50, "content_summary": "谈到 80"}],
            role="你是 buyer-fish, 你的职责是讨价还价",
            is_must_reply=True,
        )
        # 验证 prompt 包含 role + session
        msgs = client.calls[0]["messages"]
        prompt = msgs[1]["content"]
        assert "buyer-fish" in prompt
        assert "买鱼" in prompt
        assert "s1" in prompt
        # 邮箱路径: prompt 明确说"必须回复", "continue" 和 "new" 都有
        assert "必须回复" in prompt
        assert "continue" in prompt
        assert "new" in prompt

    @pytest.mark.asyncio
    async def test_prompt_轮询_含_skip_选项(self, cfg):
        """轮询路径 prompt 含 skip 选项."""
        client = MockLLMClient(response='{"action": "skip"}')
        dm = DecisionMaker(cfg, client=client)
        await dm.decide(
            mail={"content": "test", "path": "poll"},
            sessions=[], role="bot", is_must_reply=False,
        )
        prompt = client.calls[0]["messages"][1]["content"]
        assert "skip" in prompt

    @pytest.mark.asyncio
    async def test_no_client_raises(self, cfg):
        """没 client (config invalid) → decide 抛 RuntimeError."""
        dm = DecisionMaker(DecisionConfig())  # 默认无 api_key
        with pytest.raises(RuntimeError, match="not ready"):
            await dm.decide(mail={}, sessions=[], role="", is_must_reply=True)


# =============================================================================
# EventHandler 集成
# =============================================================================


class TestEventHandlerIntegration:
    """EventHandler.handle_mail 集成 DecisionMaker."""

    @pytest.fixture
    def env(self, tmp_path):
        """env: 临时 data_dir + mock CLI + 预设 sessions."""
        from agents_chat.v2.core.agent import Agent
        from agents_chat.v2.infra.cli import MockCLI
        from agents_chat.v2.core.session_manager import SessionManager
        data_dir = tmp_path / "data"
        for sub in ["channels", "mailboxes", "sessions", "locks", "workspaces"]:
            (data_dir / sub).mkdir(parents=True)
        # 预设 1 个 session
        sm = SessionManager(data_dir / "sessions" / "bot.json", "bot")
        sm.create(topic="买鱼", channel="general", task_id="task_1")
        agent = Agent(
            agent_id="bot", cli=MockCLI(), data_dir=data_dir,
            default_channel="general",
        )
        ch = agent.channel("general")
        ch.add_member("bot")
        ch.add_member("alice")
        return {"agent": agent, "data_dir": data_dir, "sm": sm}

    @pytest.mark.asyncio
    async def test_decision_continue_调_cli(self, env):
        """DecisionMaker 决定 continue → 调 CLI 生成 reply."""
        client = MockLLMClient(response='{"action": "continue", "session_id": "local_bot_1"}')
        env["agent"].event_handler.decision_maker = DecisionMaker(
            env["agent"].event_handler.decision_maker.config,
            client=client,
        )
        env["agent"].mailbox.append(
            type="mention", content="@bot 续聊", channel="general", ref_msg_id="",
            extra={"path": "email"},
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # 频道里应该有 reply
        ch = env["agent"].channel("general")
        msgs = ch.tail(10)
        reply_msgs = [m for m in msgs if m.get("type") == "reply"]
        assert len(reply_msgs) >= 1

    @pytest.mark.asyncio
    async def test_decision_new_新建_session_调_cli(self, env):
        """DecisionMaker 决定 new → 新建 session + 调 CLI."""
        client = MockLLMClient(response='{"action": "new"}')
        env["agent"].event_handler.decision_maker = DecisionMaker(
            env["agent"].event_handler.decision_maker.config,
            client=client,
        )
        env["agent"].mailbox.append(
            type="mention", content="@bot 新话题", channel="general", ref_msg_id="",
            extra={"path": "email"},
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # 新 session 应建
        ch = env["agent"].channel("general")
        reply_msgs = [m for m in ch.tail(10) if m.get("type") == "reply"]
        assert len(reply_msgs) >= 1

    @pytest.mark.asyncio
    async def test_decision_skip_写_system_不调_cli(self, env):
        """DecisionMaker 决定 skip → 写 system 消息, 不调 CLI."""
        client = MockLLMClient(response='{"action": "skip", "reason": "不相关"}')
        env["agent"].event_handler.decision_maker = DecisionMaker(
            env["agent"].event_handler.decision_maker.config,
            client=client,
        )
        env["agent"].mailbox.append(
            type="task_broadcast", content="[TASK] 不相关", channel="general", ref_msg_id="",
            extra={"path": "poll"},
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # 频道应该有 system 消息, 不应有 reply
        ch = env["agent"].channel("general")
        msgs = ch.tail(10)
        system_msgs = [m for m in msgs if m.get("type") == "system"]
        reply_msgs = [m for m in msgs if m.get("type") == "reply"]
        assert len(system_msgs) >= 1
        assert "ignored" in system_msgs[-1]["content"].lower() or "忽略" in system_msgs[-1]["content"]
        assert len(reply_msgs) == 0

    @pytest.mark.asyncio
    async def test_邮箱路径_skip_强制_new_调_cli(self, env):
        """邮箱路径 LLM 返回 skip → 强制改 new → 调 CLI."""
        client = MockLLMClient(response='{"action": "skip"}')  # LLM 想 skip
        env["agent"].event_handler.decision_maker = DecisionMaker(
            env["agent"].event_handler.decision_maker.config,
            client=client,
        )
        env["agent"].mailbox.append(
            type="mention", content="@bot 你好", channel="general", ref_msg_id="",
            extra={"path": "email"},  # 邮箱路径, 必答
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # 应该有 reply (强制 new)
        ch = env["agent"].channel("general")
        reply_msgs = [m for m in ch.tail(10) if m.get("type") == "reply"]
        assert len(reply_msgs) >= 1

    @pytest.mark.asyncio
    async def test_LLM_失败_fallback_session_manager(self, env):
        """DecisionMaker LLM 抛异常 → fallback SessionManager.decide_session."""
        client = MockLLMClient(raise_exc=RuntimeError("network down"))
        env["agent"].event_handler.decision_maker = DecisionMaker(
            env["agent"].event_handler.decision_maker.config,
            client=client,
        )
        env["agent"].mailbox.append(
            type="mention", content="@bot hi", channel="general", ref_msg_id="",
            extra={"path": "email"},
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # fallback 成功, 应该有 reply
        ch = env["agent"].channel("general")
        reply_msgs = [m for m in ch.tail(10) if m.get("type") == "reply"]
        assert len(reply_msgs) >= 1

    @pytest.mark.asyncio
    async def test_no_decision_maker_fallback(self, env):
        """没传 DecisionMaker (config invalid) → fallback SessionManager."""
        # 强制把 decision_maker 设为 invalid
        env["agent"].event_handler.decision_maker = DecisionMaker(DecisionConfig())
        env["agent"].mailbox.append(
            type="mention", content="@bot hi", channel="general", ref_msg_id="",
            extra={"path": "email"},
        )
        mails = env["agent"].mailbox.read_and_clear()
        await env["agent"].event_handler.handle_mail(mails[0])
        # fallback 走 SessionManager.decide_session
        ch = env["agent"].channel("general")
        reply_msgs = [m for m in ch.tail(10) if m.get("type") == "reply"]
        assert len(reply_msgs) >= 1


# =============================================================================
# Scanner 投递 path 字段
# =============================================================================

