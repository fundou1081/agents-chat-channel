"""Tests for v2.0 FastAPI Server.

覆盖:
  - health / root / stats
  - agents (list / get / start / stop / tick / log)
  - channels (list / messages / post / meta / add_member / add_admin)
  - mailboxes (get / clear)
  - sessions (list / active / decide)
  - state_board (list / get_task)
  - scanner status
  - processes (list / get / stop)
  - start/stop agent e2e

目标: ≥20 tests, 全部过.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def data_dir(tmp_path):
    """临时 data_dir, 已建子目录."""
    d = tmp_path / "data"
    for sub in ["channels", "mailboxes", "sessions", "locks", "workspaces"]:
        (d / sub).mkdir(parents=True)
    return d


@pytest.fixture
def client(data_dir):
    """FastAPI TestClient."""
    from fastapi.testclient import TestClient
    from agents_chat.v2.server import create_app
    app = create_app(data_dir=data_dir, host="127.0.0.1", port=0)
    return TestClient(app)


# =============================================================================
# Health / Root
# =============================================================================


class TestHealth:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "data_dir" in d
        assert "endpoints" in d

    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "ts" in d


# =============================================================================
# Stats
# =============================================================================


class TestStats:
    def test_empty_stats(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200
        d = r.json()
        assert d["channels"] == 0
        assert d["agents"] == 0
        assert d["total_messages"] == 0
        assert d["total_mails"] == 0
        assert d["total_sessions"] == 0
        assert d["running"]["agents"] == 0
        assert d["running"]["scanner"] is False
        assert d["running"]["scheduler"] is False

    def test_stats_with_data(self, client, data_dir):
        # 写 1 个频道, 1 个 mailbox, 1 个 session
        (data_dir / "channels" / "general.jsonl").write_text(
            json.dumps({"id": "ch_1", "content": "hi"}) + "\n"
        )
        (data_dir / "mailboxes" / "bot.json").write_text(
            json.dumps({"agent": "bot", "pending": [{"content": "a"}, {"content": "b"}]})
        )
        sess_data = {"sessions": {"s1": {
            "session_id": "s1", "topic": "test", "channel": "general",
            "progress": 0, "status": "active",
        }}}
        (data_dir / "sessions" / "bot.json").write_text(json.dumps(sess_data))
        r = client.get("/api/stats")
        d = r.json()
        assert d["channels"] == 1
        assert d["agents"] == 1
        assert d["total_messages"] == 1
        assert d["total_mails"] == 2
        assert d["total_sessions"] == 1


# =============================================================================
# Agents
# =============================================================================


class TestAgents:
    def test_list_empty(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        d = r.json()
        assert d["agents"] == []
        assert d["count"] == 0

    def test_list_with_mailbox(self, client, data_dir):
        (data_dir / "mailboxes" / "bot1.json").touch()
        (data_dir / "mailboxes" / "bot2.json").touch()
        r = client.get("/api/agents")
        d = r.json()
        assert d["count"] == 2
        assert {a["agent_id"] for a in d["agents"]} == {"bot1", "bot2"}

    def test_get_agent_not_found(self, client):
        r = client.get("/api/agents/nobody")
        assert r.status_code == 404

    def test_get_agent(self, client, data_dir):
        (data_dir / "mailboxes" / "bot.json").write_text(
            json.dumps({"agent": "bot", "pending": [{"content": "x"}]})
        )
        r = client.get("/api/agents/bot")
        assert r.status_code == 200
        d = r.json()
        assert d["agent_id"] == "bot"
        assert d["mailbox_count"] == 1
        assert d["process"] is None  # 没启动进程

    def test_tick_agent_not_found(self, client):
        r = client.post("/api/agents/nobody/tick")
        assert r.status_code == 404

    def test_tick_agent(self, client, data_dir):
        (data_dir / "mailboxes" / "bot.json").touch()
        r = client.post("/api/agents/bot/tick")
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["tick_sent"] is True
        # 邮箱里应该有 1 封 system_notify
        from agents_chat.v2.files.mailbox import Mailbox
        mb = Mailbox(data_dir / "mailboxes" / "bot.json", "bot")
        mails = mb.peek()
        assert len(mails) == 1
        assert mails[0]["type"] == "system_notify"

    def test_start_agent(self, client, data_dir):
        """启动 mock agent 进程, 等 0.5s 看进程状态."""
        r = client.post("/api/agents/test-agent/start", json={
            "cli": "mock",
            "channel": "general",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert d["process"]["agent_id"] == "test-agent"
        assert d["process"]["cli"] == "mock"
        assert d["process"]["pid"] > 0
        assert d["process"]["exit_code"] == -1  # 还在跑

    def test_start_agent_duplicate(self, client, data_dir):
        r1 = client.post("/api/agents/dup/start", json={"cli": "mock"})
        assert r1.status_code == 200
        # 立即再启动 → 409
        r2 = client.post("/api/agents/dup/start", json={"cli": "mock"})
        assert r2.status_code == 409

    def test_stop_agent_not_running(self, client):
        r = client.post("/api/agents/nobody/stop")
        assert r.status_code == 404


# =============================================================================
# Channels
# =============================================================================


class TestChannels:
    def test_list_empty(self, client):
        r = client.get("/api/channels")
        d = r.json()
        assert d["channels"] == []
        assert d["count"] == 0

    def test_list_with_channel(self, client, data_dir):
        (data_dir / "channels" / "general.jsonl").touch()
        r = client.get("/api/channels")
        d = r.json()
        assert d["count"] == 1
        assert d["channels"][0]["name"] == "general"
        assert d["channels"][0]["messages"] == 0

    def test_post_message(self, client, data_dir):
        r = client.post("/api/channels/general/messages", json={
            "content": "@bot hello",
            "from": "alice",
            "mentions": ["bot"],
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is True
        assert "msg_id" in d
        # 频道里应该能看到
        r2 = client.get("/api/channels/general/messages")
        msgs = r2.json()["messages"]
        assert any(m["content"] == "@bot hello" for m in msgs)

    def test_get_messages(self, client, data_dir):
        ch_path = data_dir / "channels" / "general.jsonl"
        ch_path.write_text(
            json.dumps({"id": "1", "content": "a", "from": "alice", "ts": "2026-01-01"}) + "\n"
            + json.dumps({"id": "2", "content": "b", "from": "bob", "ts": "2026-01-02"}) + "\n"
        )
        r = client.get("/api/channels/general/messages")
        d = r.json()
        assert d["count"] == 2

    def test_get_messages_with_limit(self, client, data_dir):
        ch_path = data_dir / "channels" / "general.jsonl"
        lines = [json.dumps({"id": str(i), "content": f"msg{i}"}) + "\n" for i in range(20)]
        ch_path.write_text("".join(lines))
        r = client.get("/api/channels/general/messages?limit=5")
        d = r.json()
        assert d["count"] == 5

    def test_get_messages_not_found(self, client):
        r = client.get("/api/channels/nobody/messages")
        assert r.status_code == 404

    def test_get_meta(self, client, data_dir):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(data_dir / "channels" / "general.jsonl", "general")
        ch.add_member("alice")
        ch.add_admin("bot")
        r = client.get("/api/channels/general/meta")
        d = r.json()
        assert "alice" in d["members"]
        assert "bot" in d["admins"]

    def test_add_member(self, client, data_dir):
        r = client.post("/api/channels/general/members", json={"agent_id": "alice"})
        assert r.status_code == 200
        d = r.json()
        assert d["added"] is True
        assert "alice" in d["members"]

    def test_add_admin_worker(self, client, data_dir):
        r = client.post("/api/channels/general/admins", json={
            "agent_id": "bot", "is_worker": True,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["added"] is True
        assert "bot" in d["admins"]

    def test_add_admin_human(self, client, data_dir):
        r = client.post("/api/channels/general/admins", json={
            "agent_id": "user_ou_abc", "is_worker": False,
        })
        d = r.json()
        assert "user_ou_abc" in d["human_admins"]
        assert "user_ou_abc" not in d["admins"]


# =============================================================================
# Mailboxes
# =============================================================================


class TestMailboxes:
    def test_get_mailbox_not_found(self, client):
        r = client.get("/api/mailboxes/nobody")
        assert r.status_code == 404

    def test_get_mailbox(self, client, data_dir):
        from agents_chat.v2.files.mailbox import Mailbox
        mb = Mailbox(data_dir / "mailboxes" / "bot.json", "bot")
        mb.append(type="mention", content="@bot hi", channel="general")
        mb.append(type="task_broadcast", content="[TASK] do it", channel="general")
        r = client.get("/api/mailboxes/bot")
        d = r.json()
        assert d["count"] == 2
        assert len(d["mails"]) == 2

    def test_clear_mailbox(self, client, data_dir):
        from agents_chat.v2.files.mailbox import Mailbox
        mb = Mailbox(data_dir / "mailboxes" / "bot.json", "bot")
        mb.append(type="mention", content="@bot hi", channel="general")
        r = client.delete("/api/mailboxes/bot")
        d = r.json()
        assert d["ok"] is True
        assert d["cleared"] == 1
        # 邮箱已空
        assert mb.count() == 0


# =============================================================================
# Sessions
# =============================================================================


class TestSessions:
    def test_list_empty(self, client):
        r = client.get("/api/sessions/bot")
        d = r.json()
        assert d["count"] == 0
        assert d["sessions"] == []

    def test_list_sessions(self, client, data_dir):
        from agents_chat.v2.session_manager import SessionManager
        sm = SessionManager(data_dir / "sessions" / "bot.json", "bot")
        sm.create(topic="t1", channel="general", task_id="task_1")
        r = client.get("/api/sessions/bot")
        d = r.json()
        assert d["count"] == 1

    def test_active_sessions(self, client, data_dir):
        from agents_chat.v2.session_manager import SessionManager
        sm = SessionManager(data_dir / "sessions" / "bot.json", "bot")
        sm.create(topic="t1", channel="general", task_id="task_1")
        r = client.get("/api/sessions/bot/active")
        d = r.json()
        assert d["count"] == 1

    def test_decide_session(self, client, data_dir):
        r = client.post("/api/sessions/bot/decide", json={
            "task_id": "task_1", "topic": "买鱼", "channel": "general",
        })
        assert r.status_code == 200
        d = r.json()
        assert d["is_new"] is True
        assert d["session"]["topic"] == "买鱼"
        # 第二次同 task → 续
        r2 = client.post("/api/sessions/bot/decide", json={
            "task_id": "task_1", "topic": "买鱼", "channel": "general",
        })
        d2 = r2.json()
        assert d2["is_new"] is False
        assert d2["session"]["session_id"] == d["session"]["session_id"]


# =============================================================================
# State Board
# =============================================================================


class TestStateBoard:
    def test_empty_state_board(self, client, data_dir):
        # 确保 state_board.json 不存在 (避免串扰)
        sb_file = data_dir / "state_board.json"
        if sb_file.exists():
            sb_file.unlink()
        r = client.get("/api/state_board")
        d = r.json()
        assert d["tasks"] == {}

    def test_get_task_not_found(self, client):
        r = client.get("/api/state_board/nobody")
        assert r.status_code == 404

    def test_state_board_with_data(self, client, data_dir):
        from agents_chat.v2.state_board import StateBoard
        sb = StateBoard(data_dir / "state_board.json")
        sb.update_from_status("task_1", {
            "progress": 50, "summary": "doing", "next_action": "wait", "confidence": "high",
        }, agent_id="bot")
        r = client.get("/api/state_board/task_1")
        d = r.json()
        assert d["task"]["progress"] == 50
        assert d["task"]["summary"] == "doing"


# =============================================================================
# Scanner
# =============================================================================


class TestScanner:
    def test_scanner_status_empty(self, client):
        r = client.get("/api/scanner/status")
        d = r.json()
        assert d["ok"] is True
        assert d["offsets"] == {}

    def test_scanner_status_with_data(self, client, data_dir):
        (data_dir / "scanner_state.json").write_text(json.dumps({
            "offsets": {"general": 5, "fish": 10},
            "updated_at": "2026-06-08T00:00:00Z",
        }))
        r = client.get("/api/scanner/status")
        d = r.json()
        assert d["offsets"]["general"] == 5
        assert d["offsets"]["fish"] == 10


# =============================================================================
# Processes
# =============================================================================


class TestProcesses:
    def test_list_empty(self, client):
        r = client.get("/api/processes")
        d = r.json()
        assert d["count"] == 0
        assert d["processes"] == []

    def test_get_process_not_found(self, client):
        r = client.get("/api/processes/nobody")
        assert r.status_code == 404

    def test_stop_process_not_found(self, client):
        r = client.post("/api/processes/nobody/stop")
        assert r.status_code == 404


# =============================================================================
# E2E
# =============================================================================


class TestEndToEnd:
    def test_post_message_then_mailbox(self, client, data_dir):
        """1. 发消息到频道 2. Scanner 不在 (没启动) → 邮箱没收到 (manual test only)."""
        r = client.post("/api/channels/general/messages", json={
            "content": "@bot 报个价",
            "from": "alice",
            "mentions": ["bot"],
        })
        assert r.status_code == 200
        # 频道应该有消息
        r2 = client.get("/api/channels/general/messages")
        assert r2.json()["count"] == 1

    def test_full_workflow_start_stop(self, client, data_dir):
        """启动 agent → 看状态 → 停止 agent."""
        import time
        # 启动
        r = client.post("/api/agents/worker/start", json={"cli": "mock"})
        assert r.status_code == 200
        proc_id = r.json()["process"]["process_id"]
        # 等子进程建 mailbox (要起 python 解释器, ~0.5s)
        time.sleep(1.0)
        # 看列表
        r2 = client.get("/api/processes")
        assert r2.json()["count"] >= 1
        # 看 agent 详情
        r3 = client.get("/api/agents/worker")
        assert r3.json()["process"]["pid"] > 0
        # 停
        r4 = client.post(f"/api/processes/{proc_id}/stop")
        assert r4.status_code == 200
        # 看 agent 详情 (已停)
        r5 = client.get("/api/agents/worker")
        # exit_code != -1 表示已退出
        assert r5.json()["process"]["exit_code"] != -1
