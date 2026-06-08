"""Tests for EventHandler._build_prompt 注入真频道历史."""
import pytest
from pathlib import Path

from agents_chat.v2.event_handler import EventHandler
from agents_chat.v2.cli.mock import MockCLI
from agents_chat.v2.communication import CommunicationComponent
from agents_chat.v2.files.channel import Channel
from agents_chat.v2.files.mailbox import Mailbox
from agents_chat.v2.session_manager import SessionManager
from agents_chat.v2.state_board import StateBoard


def _make_scheduler(tmp_path, channel_msgs=None):
    """构造标准测试环境: scheduler + channel + mailbox (module-level helper)."""
    mailbox = Mailbox(tmp_path / "mailboxes" / "agent1.json", "agent1")
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True, exist_ok=True)
    state_board = StateBoard(tmp_path / "state_board.json")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    sessions = SessionManager(tmp_path / "sessions" / "agent1.json", "agent1")
    cli = MockCLI()
    comms = CommunicationComponent(
        agent_id="agent1", mailbox=mailbox,
        channels_dir=channels_dir, state_board=state_board,
        lock_dir=lock_dir, default_channel="fish-market",
    )
    scheduler = EventHandler(
        comms=comms, sessions=sessions, cli=cli,
        agent_id="agent1", system_prompt="你是 agent1",
        workspace_dir=tmp_path / "ws", default_channel="fish-market",
        channels_dir=channels_dir, lock_dir=lock_dir,
    )
    if channel_msgs:
        ch = Channel(channels_dir / "fish-market.jsonl", "fish-market")
        for m in channel_msgs:
            ch.append(
                from_=m.get("from", "god"),
                content=m.get("content", ""),
                type=m.get("type", "mention"),
                mentions=m.get("mentions", []),
            )
    return scheduler


class TestBuildPromptInjection:
    """_build_prompt 应该注入真频道历史."""

    def test_prompt_includes_channel_history(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, channel_msgs=[
            {"from": "god", "content": "@agent1 @agent2 模拟砍价"},
            {"from": "agent2", "content": "80 元一斤, 不讲价"},
        ])
        session = scheduler.sessions.create(topic="鱼市砍价", channel="fish-market")
        mail = {
            "ref_msg_id": "ch_3", "type": "mention",
            "content": "@agent1 80 太贵, 70 行不行",
            "channel": "fish-market",
        }
        prompt = scheduler._build_prompt(mail, session, "task_xxx", "鱼市砍价", "fish-market")
        assert "80 元一斤, 不讲价" in prompt
        assert "频道" in prompt
        assert "真实历史" in prompt
        assert "@agent1 @agent2 模拟砍价" in prompt

    def test_prompt_counts_my_rounds(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, channel_msgs=[
            {"from": "god", "content": "开场"},
            {"from": "agent1", "content": "我第一轮: 100 元"},
            {"from": "agent2", "content": "我第一轮: 70 元"},
            {"from": "agent1", "content": "我第二轮: 80 元"},
        ])
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1 现在", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t1", "x", "fish-market")
        assert "Round 3" in prompt

    def test_prompt_includes_session_context(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        scheduler.sessions.update(
            session.session_id,
            content_delta="开价 100", progress=50, next_action="等 buyer",
        )
        session = scheduler.sessions.get(session.session_id)
        mail = {"type": "mention", "content": "@agent1 x", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "开价 100" in prompt
        assert "[进度] 50%" in prompt
        assert "[之前 next_action] 等 buyer" in prompt

    def test_prompt_explicit_forbids_narration(self, tmp_path):
        scheduler = _make_scheduler(tmp_path)
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1 x", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "禁止" in prompt
        assert "剧本" in prompt or "模拟" in prompt

    def test_prompt_truncates_long_messages(self, tmp_path):
        long_content = "x" * 500
        scheduler = _make_scheduler(tmp_path, channel_msgs=[
            {"from": "god", "content": long_content},
        ])
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "x" * 200 in prompt
        assert "x" * 250 not in prompt

    def test_prompt_handles_empty_channel(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, channel_msgs=[])
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1 hi", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "你" in prompt
        assert "先" in prompt


class TestPromptWithBargainFlow:
    """3 轮讨价还价: prompt 逐步注入历史."""

    def test_round_2_sees_round_1_history(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, channel_msgs=[
            {"from": "god", "content": "@agent1 @agent2 模拟砍价"},
            {"from": "agent1", "content": "我开价 100 元", "type": "reply"},
            {"from": "agent2", "content": "太贵, 70 块", "type": "reply"},
        ])
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1 70 行不行?", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "70 块" in prompt
        assert "Round 2" in prompt

    def test_round_3_sees_rounds_1_and_2(self, tmp_path):
        scheduler = _make_scheduler(tmp_path, channel_msgs=[
            {"from": "god", "content": "砍价"},
            {"from": "agent1", "content": "R1 卖: 100"},
            {"from": "agent2", "content": "R1 买: 70"},
            {"from": "agent1", "content": "R2 卖: 80"},
            {"from": "agent2", "content": "R2 买: 75"},
        ])
        session = scheduler.sessions.create(topic="x", channel="fish-market")
        mail = {"type": "mention", "content": "@agent1 75 行不行?", "channel": "fish-market"}
        prompt = scheduler._build_prompt(mail, session, "t", "x", "fish-market")
        assert "R1 卖: 100" in prompt
        assert "R1 买: 70" in prompt
        assert "R2 卖: 80" in prompt
        assert "R2 买: 75" in prompt
        assert "Round 3" in prompt
