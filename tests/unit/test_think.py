"""Test think/decide logic."""
import pytest
from agents_chat.author.think import _extract_json, _format_active_sessions, _format_new_mail, build_think_prompt
from agents_chat.llm.mock import MockLLM
from agents_chat.models import Mail, Persona, SessionContext, TickContext


def test_extract_json_pure():
    assert _extract_json('{"a": 1}') == '{"a": 1}'


def test_extract_json_with_markdown():
    text = 'some text\n```json\n{"a": 1}\n```\nmore'
    assert _extract_json(text) == '{"a": 1}'


def test_extract_json_embedded():
    text = 'I think {"a": 1, "b": 2} is the answer'
    assert _extract_json(text) == '{"a": 1, "b": 2}'


def test_format_new_mail_empty():
    assert _format_new_mail([]) == "(空)"


def test_format_new_mail_some():
    m = Mail.new(sender="god", recipients=["zhang"], subject="hi", body="hello world")
    out = _format_new_mail([m])
    assert "from god" in out
    assert "hi" in out


def test_format_active_sessions_empty():
    assert _format_active_sessions([]) == "(无 active session)"


def test_format_active_sessions_blocked():
    s = SessionContext(thread_id="T-1", topic="bug", status="blocked", blocked_reason="等老王")
    out = _format_active_sessions([s])
    assert "等老王" in out


def test_build_think_prompt_includes_persona():
    p = Persona(id="zhang", display_name="小张", title="前端", system_prompt="...")
    ctx = TickContext(persona=p, new_mail=[], active_sessions=[])
    system, user = build_think_prompt(ctx)
    assert "小张" in system
    assert "前端" in system
    assert "JSON" in system


@pytest.mark.asyncio
async def test_mock_llm_returns_valid_json():
    llm = MockLLM()
    p = Persona(id="zhang", display_name="小张", title="前端", system_prompt="你是小张,前端工程师")
    m = Mail.new(sender="god", recipients=["zhang"], subject="[任务] 改个bug", body="请修")
    ctx = TickContext(persona=p, new_mail=[m], active_sessions=[])
    system, user = build_think_prompt(ctx)
    decision = await llm.think(system=system, user=user, ctx=ctx)
    assert decision.thinking
    assert decision.actions is not None
    assert decision.outgoing_mail is not None
    assert decision.next_status in ("idle", "working", "blocked")
    # 有任务邮件,应该回一封
    assert len(decision.outgoing_mail) >= 1
    assert decision.outgoing_mail[0].sender == "zhang"
