"""Tests for v2.0 Gates (Worker 输入/输出过滤).

覆盖:
  - MaxLengthGate: 截断过长内容
  - SecretLeakGate: 检测 API key / 密码, 默认 sanitize (非 strict)
  - ControlCharsGate: 去除控制字符
  - GateChain: 顺序应用, 任一拒绝立即短路
  - EventHandler 集成: input/output gate 在 handle_mail 里跑通

目标: ≥25 tests, 全部过.
"""
from __future__ import annotations

import pytest

from agents_chat.infra.gates import (
    ControlCharsGate,
    GateChain,
    GateResult,
    MaxLengthGate,
    SecretLeakGate,
)


# =============================================================================
# GateResult
# =============================================================================


class TestGateResult:
    def test_allow(self):
        r = GateResult.allow("hello", "test")
        assert r.allowed is True
        assert r.text == "hello"
        assert r.gate == "test"
        assert r.reason == ""

    def test_deny(self):
        r = GateResult.deny("hi", "too short", "test")
        assert r.allowed is False
        assert r.text == "hi"
        assert r.reason == "too short"
        assert r.gate == "test"


# =============================================================================
# MaxLengthGate
# =============================================================================


class TestMaxLengthGate:
    def test_under_limit_allow(self):
        g = MaxLengthGate(max_chars=100)
        r = g.check_input("short text")
        assert r.allowed is True
        assert r.text == "short text"
        assert r.reason == ""

    def test_over_limit_truncate(self):
        g = MaxLengthGate(max_chars=10, suffix="...")
        r = g.check_input("this is a very long content")
        assert r.allowed is True  # 截断 = sanitize, 不算拒绝
        assert r.text.startswith("this is a ")
        assert r.text.endswith("...")
        assert "truncated" in r.reason

    def test_exact_limit_no_truncate(self):
        g = MaxLengthGate(max_chars=5)
        r = g.check_input("abcde")
        assert r.allowed is True
        assert r.text == "abcde"
        assert r.reason == ""

    def test_check_output_works(self):
        g = MaxLengthGate(max_chars=5)
        r = g.check_output("abcdef")
        assert r.allowed is True
        assert "[truncated]" in r.text

    def test_name_includes_max_chars(self):
        g = MaxLengthGate(max_chars=4000)
        assert "4000" in g.name


# =============================================================================
# SecretLeakGate
# =============================================================================


class TestSecretLeakGate:
    def test_openai_key_detected(self):
        g = SecretLeakGate()
        text = "my key is sk-abcdefghijklmnopqrstuvwxyz"
        r = g.check_input(text)
        assert r.allowed is True  # 默认 sanitize
        assert "REDACTED" in r.text
        assert "openai" in r.reason

    def test_anthropic_key_detected(self):
        g = SecretLeakGate()
        text = "sk-ant-abcdefghijklmnopqrstuvwxyz123"
        r = g.check_input(text)
        assert "[REDACTED:anthropic key]" in r.text

    def test_aws_access_key_detected(self):
        g = SecretLeakGate()
        text = "AKIAIOSFODNN7EXAMPLE"
        r = g.check_input(text)
        assert "[REDACTED:aws access key]" in r.text

    def test_github_token_detected(self):
        g = SecretLeakGate()
        text = "ghp_abcdefghijklmnopqrstuvwxyz0123456789ABCD"
        r = g.check_input(text)
        assert "[REDACTED:github token]" in r.text

    def test_bearer_token_detected(self):
        g = SecretLeakGate()
        text = "Authorization: Bearer abcdefghijklmnopqrstuv"
        r = g.check_input(text)
        assert "[REDACTED:bearer token]" in r.text

    def test_password_pattern_detected(self):
        g = SecretLeakGate()
        text = "password=MySecret123Pass"
        r = g.check_input(text)
        assert "[REDACTED:password]" in r.text

    def test_private_key_detected(self):
        g = SecretLeakGate()
        text = "-----BEGIN RSA PRIVATE KEY-----"
        r = g.check_input(text)
        assert "[REDACTED:private key]" in r.text

    def test_no_secrets_allow(self):
        g = SecretLeakGate()
        text = "hello world, this is safe content"
        r = g.check_input(text)
        assert r.allowed is True
        assert r.text == text
        assert r.reason == ""

    def test_strict_mode_denies(self):
        g = SecretLeakGate(strict=True)
        text = "my key is sk-abcdefghijklmnopqrstuvwxyz"
        r = g.check_input(text)
        assert r.allowed is False
        assert "openai" in r.reason

    def test_check_output_works(self):
        g = SecretLeakGate()
        text = "out: sk-abcdefghijklmnopqrstuvwxyz"
        r = g.check_output(text)
        assert "[REDACTED" in r.text


# =============================================================================
# ControlCharsGate
# =============================================================================


class TestControlCharsGate:
    def test_keep_whitespace(self):
        g = ControlCharsGate(keep_whitespace=True)
        text = "line1\nline2\ttabbed\r\nwindows"
        r = g.check_input(text)
        assert r.allowed is True
        assert r.text == text
        assert r.reason == ""

    def test_remove_nul(self):
        g = ControlCharsGate(keep_whitespace=True)
        text = "before\x00after"
        r = g.check_input(text)
        assert r.allowed is True
        assert "\x00" not in r.text
        assert r.text == "beforeafter"
        assert "removed 1" in r.reason

    def test_remove_soh(self):
        g = ControlCharsGate(keep_whitespace=True)
        text = "a\x01b\x02c"
        r = g.check_input(text)
        assert "\x01" not in r.text
        assert "\x02" not in r.text
        assert r.text == "abc"

    def test_strip_whitespace_when_disabled(self):
        g = ControlCharsGate(keep_whitespace=False)
        text = "line1\nline2"
        r = g.check_input(text)
        # \n 也被去掉
        assert "\n" not in r.text
        assert r.text == "line1line2"

    def test_check_output_works(self):
        g = ControlCharsGate(keep_whitespace=True)
        text = "out\x00text"
        r = g.check_output(text)
        assert r.text == "outtext"


# =============================================================================
# GateChain
# =============================================================================


class TestGateChain:
    def test_empty_chain_allow(self):
        ch = GateChain(gates=[], direction="input")
        r = ch.run("hello")
        assert r.allowed is True
        assert r.text == "hello"
        assert r.gate == "chain"

    def test_single_chain_allow(self):
        ch = GateChain(gates=[MaxLengthGate(max_chars=10)], direction="input")
        r = ch.run("short")
        assert r.allowed is True
        assert r.text == "short"

    def test_chain_order_pipeline(self):
        """第一个 gate 改写后, 第二个 gate 看到的是改写后的."""
        gates = [
            MaxLengthGate(max_chars=100, suffix="..."),  # 100 chars, 不截断这个 text
            SecretLeakGate(),
        ]
        ch = GateChain(gates=gates, direction="input")
        # 短 secret 字符串, 第一个 gate 不截断, 第二个 gate sanitize
        text = "key=sk-abcdefghijklmnopqrstuv"
        r = ch.run(text)
        assert r.allowed is True
        assert "[REDACTED" in r.text

    def test_chain_short_circuit_deny(self):
        """第一个 gate deny 后, 第二个 gate 不跑."""
        class DenyGate:
            name = "deny_first"
            def check_input(self, text):
                return GateResult.deny(text, "blocked", "deny_first")
            def check_output(self, text):
                return GateResult.deny(text, "blocked", "deny_first")

        class TrackGate:
            """记录是否被调用."""
            name = "track"
            called = 0
            def check_input(self, text):
                TrackGate.called += 1
                return GateResult.allow(text, "track")
            def check_output(self, text):
                TrackGate.called += 1
                return GateResult.allow(text, "track")

        TrackGate.called = 0
        ch = GateChain(gates=[DenyGate(), TrackGate()], direction="input")
        r = ch.run("hello")
        assert r.allowed is False
        assert "deny_first" in r.reason
        assert TrackGate.called == 0  # 第二个 gate 没跑

    def test_chain_direction(self):
        """direction=output 时, 只调 check_output."""
        class OnlyInputGate:
            name = "only_input"
            def check_input(self, text):
                return GateResult.deny(text, "input-only", "only_input")
            def check_output(self, text):
                return GateResult.allow(text, "only_input")

        # direction=output, 调 check_output, 返回 allow
        ch = GateChain(gates=[OnlyInputGate()], direction="output")
        r = ch.run("test")
        assert r.allowed is True

        # direction=input, 调 check_input, 返回 deny
        ch2 = GateChain(gates=[OnlyInputGate()], direction="input")
        r2 = ch2.run("test")
        assert r2.allowed is False

    def test_chain_len_and_bool(self):
        ch1 = GateChain(gates=[], direction="input")
        assert len(ch1) == 0
        assert not bool(ch1)

        ch2 = GateChain(gates=[MaxLengthGate(max_chars=10)], direction="input")
        assert len(ch2) == 1
        assert bool(ch2)

    def test_chain_skips_missing_direction(self):
        """gate 没实现当前 direction, 跳过."""
        class OnlyOutputGate:
            name = "only_output"
            def check_output(self, text):
                return GateResult.allow(text, "only_output")
            # 没 check_input

        ch = GateChain(gates=[OnlyOutputGate()], direction="input")
        r = ch.run("test")
        # gate 跳过, chain 跑空, allow
        assert r.allowed is True


# =============================================================================
# EventHandler 集成 (用 mock CLI)
# =============================================================================


class TestSchedulerGateIntegration:
    """测试 EventHandler.handle_mail 集成 gates."""

    @pytest.fixture
    def tmp_data(self, tmp_path):
        """临时 data_dir, 含一个 MockCLI + 1 个 agent."""
        from agents_chat.core.agent import Agent
        from agents_chat.infra.cli import MockCLI
        agent = Agent(
            agent_id="gated",
            cli=MockCLI(),
            data_dir=tmp_path / "data",
            default_channel="general",
            system_prompt="",
        )
        # 注册一个 sender agent (有 mailbox)
        sender = Agent(
            agent_id="sender",
            cli=MockCLI(),
            data_dir=tmp_path / "data",
            default_channel="general",
        )
        # 加 sender 到 general 频道
        ch = agent.channel("general")
        ch.add_member("sender")
        ch.add_member("gated")
        return {"agent": agent, "sender": sender, "data_dir": tmp_path / "data"}

    @pytest.mark.asyncio
    async def test_input_gate_rejects_no_llm_call(self, tmp_data):
        """input gate 拒绝时, 不调 LLM, 写 system 消息."""
        from agents_chat.infra.gates import Gate, GateResult

        class DenyAll:
            name = "deny_all"
            def check_input(self, text):
                return GateResult.deny(text, "denied by test", "deny_all")
            def check_output(self, text):
                return GateResult.allow(text, "deny_all")

        # 重建 agent, 加 input gate
        from agents_chat.core.agent import Agent
        from agents_chat.infra.cli import MockCLI
        data_dir = tmp_data["data_dir"]
        agent = Agent(
            agent_id="gated",
            cli=MockCLI(),
            data_dir=data_dir,
            default_channel="general",
            input_gates=[DenyAll()],
        )
        # 把 sender 加到频道
        ch = agent.channel("general")
        ch.add_member("sender")
        ch.add_member("gated")

        # 投递 mail 到 gated
        agent.mailbox.append(
            type="mention",
            content="@gated 给我报个价",
            channel="general",
            ref_msg_id="",
        )
        # 跑一次 handle_mail (不等 run 循环)
        mails = agent.mailbox.read_and_clear()
        assert len(mails) >= 1
        mail = mails[0]
        await agent.scheduler.handle_mail(mail)

        # 验证: 频道里有 system 消息, 没 reply
        msgs = ch.tail(20)
        system_msgs = [m for m in msgs if m.get("type") == "system"]
        reply_msgs = [m for m in msgs if m.get("type") == "reply"]
        assert len(system_msgs) >= 1
        assert "gate REJECTED" in system_msgs[0]["content"]
        # reply 应该是 0 (LLM 没跑)
        assert len(reply_msgs) == 0

    @pytest.mark.asyncio
    async def test_output_gate_sanitizes(self, tmp_data):
        """output gate 改写后, 频道里看到的是改写版."""
        from agents_chat.infra.gates import Gate, GateResult

        class AppendTag:
            name = "append_tag"
            def check_input(self, text):
                return GateResult.allow(text, "append_tag")
            def check_output(self, text):
                return GateResult.allow(text + "\n[sanitized]", "append_tag")

        from agents_chat.core.agent import Agent
        from agents_chat.infra.cli import MockCLI
        data_dir = tmp_data["data_dir"]
        agent = Agent(
            agent_id="gated",
            cli=MockCLI(),
            data_dir=data_dir,
            default_channel="general",
            output_gates=[AppendTag()],
        )
        ch = agent.channel("general")
        ch.add_member("sender")
        ch.add_member("gated")

        agent.mailbox.append(
            type="mention",
            content="@gated 测试",
            channel="general",
            ref_msg_id="",
        )
        mails = agent.mailbox.read_and_clear()
        assert len(mails) >= 1
        mail = mails[0]
        await agent.scheduler.handle_mail(mail)

        msgs = ch.tail(20)
        reply_msgs = [m for m in msgs if m.get("type") == "reply"]
        assert len(reply_msgs) == 1
        assert "[sanitized]" in reply_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_no_gates_default_allow(self, tmp_data):
        """不传 gates = 旧行为, 正常 reply."""
        from agents_chat.core.agent import Agent
        from agents_chat.infra.cli import MockCLI
        data_dir = tmp_data["data_dir"]
        agent = Agent(
            agent_id="gated",
            cli=MockCLI(),
            data_dir=data_dir,
            default_channel="general",
        )
        ch = agent.channel("general")
        ch.add_member("sender")
        ch.add_member("gated")

        agent.mailbox.append(
            type="mention",
            content="@gated 测试",
            channel="general",
            ref_msg_id="",
        )
        mails = agent.mailbox.read_and_clear()
        assert len(mails) >= 1
        mail = mails[0]
        await agent.scheduler.handle_mail(mail)

        msgs = ch.tail(20)
        reply_msgs = [m for m in msgs if m.get("type") == "reply"]
        assert len(reply_msgs) == 1
