"""独立 tests for v2.0 CommunicationComponent."""
import asyncio
import pytest
import time
from pathlib import Path

from agents_chat.core.communication import CommunicationComponent
from agents_chat.infra.files import Channel
from agents_chat.infra.files import Mailbox
from agents_chat.infra.state_board import StateBoard


@pytest.fixture
def env(tmp_path):
    """标准测试环境: mailbox + channels + state_board + comms."""
    mailbox = Mailbox(tmp_path / "mailboxes" / "agent1.json", "agent1")
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True, exist_ok=True)
    state_board = StateBoard(tmp_path / "state_board.json")
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    comms = CommunicationComponent(
        agent_id="agent1",
        mailbox=mailbox,
        channels_dir=channels_dir,
        state_board=state_board,
        lock_dir=lock_dir,
        default_channel="general",
        poll_interval=0.1,  # 加速测试
    )
    return {
        "tmp": tmp_path,
        "mailbox": mailbox,
        "channels_dir": channels_dir,
        "state_board": state_board,
        "lock_dir": lock_dir,
        "comms": comms,
    }


class TestPollActive:
    def test_poll_new_mails_empty(self, env):
        assert env["comms"].poll_new_mails() == []

    def test_poll_new_mails_returns_pending(self, env):
        env["mailbox"].append(
            ref_msg_id="ch_1", type="mention",
            content="hi", channel="general",
        )
        mails = env["comms"].poll_new_mails()
        assert len(mails) == 1
        assert mails[0]["content"] == "hi"
        # 再次 poll 应该空 (已 read_and_clear)
        assert env["comms"].poll_new_mails() == []

    def test_poll_my_active_tasks(self, env):
        env["state_board"].claim("t1", "agent1", "sess1", channel="general")
        env["state_board"].claim("t2", "agent1", "sess1", channel="general")
        env["state_board"].claim("t3", "other-agent", "sess1", channel="general")
        mine = env["comms"].poll_my_active_tasks()
        assert len(mine) == 2
        assert {t["task_id"] for t in mine} == {"t1", "t2"}

    def test_poll_my_active_tasks_empty(self, env):
        assert env["comms"].poll_my_active_tasks() == []

    def test_poll_stale_tasks_filters_mine(self, env):
        env["state_board"].claim("t1", "agent1", "sess1", channel="general")
        env["state_board"].claim("t2", "other-agent", "sess1", channel="general")
        # t1 heartbeat 改旧
        import json
        from datetime import datetime, timezone, timedelta
        all_tasks = env["state_board"].list_all()
        all_tasks["t1"]["heartbeat"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1000)
        ).isoformat()
        (env["tmp"] / "state_board.json").write_text(json.dumps(all_tasks))
        sb2 = StateBoard(env["tmp"] / "state_board.json")
        env["state_board"] = sb2
        env["comms"] = CommunicationComponent(
            agent_id="agent1",
            mailbox=env["mailbox"],
            channels_dir=env["channels_dir"],
            state_board=sb2,
            lock_dir=env["lock_dir"],
            default_channel="general",
            poll_interval=0.1,
            stale_ttl=300,
        )
        stale = env["comms"].poll_stale_tasks()
        assert len(stale) == 1
        assert stale[0]["task_id"] == "t1"

    def test_poll_recent_channel(self, env):
        ch = Channel(env["channels_dir"] / "general.jsonl", "general")
        ch.append(from_="alice", content="msg1", type="mention")
        ch.append(from_="bob", content="msg2", type="reply")
        msgs, new_off = env["comms"].poll_recent_channel("general", since_offset=0)
        assert len(msgs) == 2
        assert new_off == 2

    def test_poll_channel_members(self, env):
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_member("seller")
        ch.add_member("buyer")
        members = env["comms"].poll_channel_members("fish")
        assert "god" in members
        assert "seller" in members
        assert "buyer" in members


class TestRelevantFilter:
    def test_mention_is_relevant(self, env):
        mail = {"type": "mention", "content": "@agent1 hi"}
        assert env["comms"].is_relevant_mail(mail)

    def test_task_broadcast_is_relevant(self, env):
        mail = {"type": "task_broadcast", "content": "[TASK] do x"}
        assert env["comms"].is_relevant_mail(mail)

    def test_system_notify_is_relevant(self, env):
        mail = {"type": "system_notify", "content": "ack"}
        assert env["comms"].is_relevant_mail(mail)

    def test_request_status_for_my_task_relevant(self, env):
        env["state_board"].claim("t1", "agent1", "sess", channel="general")
        mail = {"type": "request_status", "extra": {"task_id": "t1"}}
        assert env["comms"].is_relevant_mail(mail)

    def test_request_status_for_others_task_irrelevant(self, env):
        env["state_board"].claim("t1", "other-agent", "sess", channel="general")
        mail = {"type": "request_status", "extra": {"task_id": "t1"}}
        assert not env["comms"].is_relevant_mail(mail)

    def test_unknown_type_irrelevant(self, env):
        mail = {"type": "spam", "content": "..."}
        assert not env["comms"].is_relevant_mail(mail)

    def test_filter_relevant_batch(self, env):
        env["state_board"].claim("t1", "agent1", "sess", channel="general")
        mails = [
            {"type": "mention", "content": "1"},
            {"type": "task_broadcast", "content": "2"},
            {"type": "request_status", "extra": {"task_id": "t1"}},
            {"type": "request_status", "extra": {"task_id": "t-other"}},
            {"type": "spam", "content": "5"},
        ]
        relevant = env["comms"].filter_relevant(mails)
        assert len(relevant) == 3
        assert all(m["type"] != "spam" for m in relevant)


class TestPushEvents:
    def test_on_new_mail_wakes(self, env):
        """on_new_mail 调一次, _new_mail_event 应该被 set."""
        assert not env["comms"]._new_mail_event.is_set()
        env["comms"].on_new_mail()
        assert env["comms"]._new_mail_event.is_set()

    def test_stop_sets_events(self, env):
        env["comms"].stop()
        assert env["comms"]._stop_event.is_set()
        assert env["comms"]._new_mail_event.is_set()


class TestListenLoop:
    @pytest.mark.asyncio
    async def test_listen_yields_initial_active_tasks(self, env):
        """listen 启动时先 yield active tasks."""
        env["state_board"].claim("t1", "agent1", "s", channel="general")
        env["state_board"].claim("t2", "agent1", "s", channel="general")
        env["comms"].poll_interval = 0.1

        # 收集前几个事件
        events = []
        async def collect():
            count = 0
            async for et, data in env["comms"].listen():
                events.append((et, data))
                count += 1
                if count >= 2:  # 拿到 2 个 active_task 就停
                    env["comms"].stop()
                    break
        await asyncio.wait_for(collect(), timeout=2.0)
        assert len(events) == 2
        for et, _ in events:
            assert et == "active_task"

    @pytest.mark.asyncio
    async def test_listen_yields_new_mails(self, env):
        """外部 append mail, listen 主动 poll 时 yield."""
        env["comms"].poll_interval = 0.1

        async def push_mail():
            await asyncio.sleep(0.15)  # 等 listen 启动
            env["mailbox"].append(
                ref_msg_id="ch_1", type="mention",
                content="test", channel="general",
            )

        asyncio.create_task(push_mail())

        events = []
        async def collect():
            count = 0
            async for et, data in env["comms"].listen():
                events.append((et, data))
                count += 1
                if count >= 1:
                    env["comms"].stop()
                    break
        await asyncio.wait_for(collect(), timeout=2.0)
        assert events[0][0] == "mail"
        assert events[0][1]["content"] == "test"

    @pytest.mark.asyncio
    async def test_listen_filters_irrelevant_mails(self, env):
        """spam 类型的 mail 不会 yield."""
        env["comms"].poll_interval = 0.1

        async def push():
            await asyncio.sleep(0.15)
            env["mailbox"].append(
                ref_msg_id="ch_1", type="spam",
                content="...", channel="general",
            )
            env["mailbox"].append(
                ref_msg_id="ch_2", type="mention",
                content="@agent1 real", channel="general",
            )
        asyncio.create_task(push())

        events = []
        async def collect():
            count = 0
            async for et, data in env["comms"].listen():
                events.append((et, data))
                count += 1
                if count >= 1:
                    env["comms"].stop()
                    break
        await asyncio.wait_for(collect(), timeout=2.0)
        # 只 yield mention, 不 yield spam
        assert events[0][0] == "mail"
        assert events[0][1]["content"] == "@agent1 real"

    @pytest.mark.asyncio
    async def test_listen_yields_stale_tasks(self, env):
        """stale task (我持有) 会被 yield."""
        import json
        from datetime import datetime, timezone, timedelta
        env["comms"].poll_interval = 0.1
        env["comms"].stale_ttl = 60

        # 制造 stale task
        env["state_board"].claim("t-stale", "agent1", "s", channel="general")
        all_tasks = env["state_board"].list_all()
        all_tasks["t-stale"]["heartbeat"] = (
            datetime.now(timezone.utc) - timedelta(seconds=100)
        ).isoformat()
        (env["tmp"] / "state_board.json").write_text(json.dumps(all_tasks))
        # 重建 sb 和 comms
        env["state_board"] = StateBoard(env["tmp"] / "state_board.json")
        env["comms"].state_board = env["state_board"]

        events = []
        async def collect():
            count = 0
            async for et, data in env["comms"].listen():
                events.append((et, data))
                count += 1
                # 拿到 stale_task 才停 (中间可能有 active_task)
                if et == "stale_task":
                    env["comms"].stop()
                    break
        await asyncio.wait_for(collect(), timeout=2.0)
        # 找到 stale_task 事件
        stale_events = [e for e in events if e[0] == "stale_task"]
        assert len(stale_events) >= 1
        assert stale_events[0][1]["task_id"] == "t-stale"

    @pytest.mark.asyncio
    async def test_listen_can_be_stopped(self, env):
        """stop() 后 listen 退出."""
        env["comms"].poll_interval = 0.1

        async def stopper():
            await asyncio.sleep(0.2)
            env["comms"].stop()
        asyncio.create_task(stopper())

        # 如果不退出会 hang, wait_for timeout = 测试失败
        await asyncio.wait_for(
            _drain(env["comms"]), timeout=2.0,
        )


async def _drain(comms):
    async for _ in comms.listen():
        pass
