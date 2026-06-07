"""独立 tests for v2.0 SessionManager."""
import pytest
import json
import time
from pathlib import Path

from agents_chat.v2.session_manager import Session, SessionManager


class TestSessionDataclass:
    def test_to_from_dict(self):
        s = Session(
            session_id="s1", topic="test",
            content_summary="hello", progress=50,
            next_action="do x", task_id="t1", channel="ch1",
        )
        d = s.to_dict()
        s2 = Session.from_dict(d)
        assert s2.session_id == s.session_id
        assert s2.topic == s.topic
        assert s2.progress == 50
        assert s2.content_summary == "hello"


class TestSessionManagerBasic:
    def test_create(self, tmp_path):
        sm = SessionManager(tmp_path / "sessions" / "agent1.json", "agent1")
        s = sm.create(topic="鱼市砍价", channel="fish-market", task_id="t1")
        assert s.session_id.startswith("local_agent1_")
        assert s.topic == "鱼市砍价"
        assert s.channel == "fish-market"
        assert s.task_id == "t1"
        assert s.status == "active"
        assert s.last_active != ""

    def test_get(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="x")
        s2 = sm.get(s.session_id)
        assert s2 is not None
        assert s2.session_id == s.session_id
        assert s2.topic == "x"

    def test_get_not_found(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        assert sm.get("nonexistent") is None

    def test_persistence(self, tmp_path):
        p = tmp_path / "s.json"
        sm1 = SessionManager(p, "a")
        s = sm1.create(topic="持久化", channel="ch", task_id="t1")
        # 重启
        sm2 = SessionManager(p, "a")
        s2 = sm2.get(s.session_id)
        assert s2 is not None
        assert s2.topic == "持久化"
        assert s2.task_id == "t1"


class TestSessionManagerUpdate:
    def test_update_progress(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        sm.update(s.session_id, progress=50)
        s2 = sm.get(s.session_id)
        assert s2.progress == 50

    def test_update_progress_clamp(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        sm.update(s.session_id, progress=150)
        s2 = sm.get(s.session_id)
        assert s2.progress == 100
        sm.update(s.session_id, progress=-10)
        s2 = sm.get(s.session_id)
        assert s2.progress == 0

    def test_update_content_delta_accumulates(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        sm.update(s.session_id, content_delta="开价 100")
        sm.update(s.session_id, content_delta="还价 70")
        s2 = sm.get(s.session_id)
        assert s2.content_summary == "开价 100; 还价 70"

    def test_update_last_active(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        before = s.last_active
        time.sleep(0.01)
        sm.update(s.session_id, next_action="do x")
        s2 = sm.get(s.session_id)
        assert s2.last_active > before

    def test_update_status(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        sm.update(s.session_id, status="completed")
        s2 = sm.get(s.session_id)
        assert s2.status == "completed"
        # list_active 不再含它
        assert s.session_id not in {x.session_id for x in sm.list_active()}

    def test_update_remote_id(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="t")
        sm.update(s.session_id, remote_id="qwen_abc123")
        s2 = sm.get(s.session_id)
        assert s2.remote_id == "qwen_abc123"

    def test_update_nonexistent_returns_none(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        assert sm.update("nonexistent", progress=50) is None


class TestSessionManagerList:
    def test_list_active(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s1 = sm.create(topic="a")
        s2 = sm.create(topic="b")
        sm.update(s2.session_id, status="completed")
        active = sm.list_active()
        assert len(active) == 1
        assert active[0].session_id == s1.session_id

    def test_list_by_channel(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        sm.create(topic="a", channel="ch1")
        sm.create(topic="b", channel="ch1")
        sm.create(topic="c", channel="ch2")
        ch1 = sm.list_by_channel("ch1")
        assert len(ch1) == 2

    def test_list_by_task(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        sm.create(topic="a", task_id="t1")
        sm.create(topic="b", task_id="t1")
        sm.create(topic="c", task_id="t2")
        t1 = sm.list_by_task("t1")
        assert len(t1) == 2

    def test_find_by_topic_keyword(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        sm.create(topic="鱼市砍价", channel="fish-market")
        s = sm.find_by_topic_keyword("fish-market", "鱼市")
        assert s is not None
        assert "鱼市" in s.topic

    def test_find_by_topic_keyword_no_match(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        sm.create(topic="鱼市", channel="fish-market")
        assert sm.find_by_topic_keyword("fish-market", "水果") is None


class TestDecideSession:
    """核心 API: 决定续/新建."""

    def test_first_time_creates(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s, is_new = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        assert is_new
        assert s.task_id == "t1"
        assert s.topic == "鱼市"

    def test_exact_match_returns_existing(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        s2, is_new = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        assert not is_new
        assert s2.session_id == s1.session_id

    def test_different_task_same_topic_reuses_session(self, tmp_path):
        """同 channel + topic 但不同 task_id → 续 (宽松策略, 共享 context).
        e.g. 鱼市 task_001 (开价) → task_002 (还价) 共享同一 session.
        """
        sm = SessionManager(tmp_path / "s.json", "a")
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        s2, is_new = sm.decide_session(task_id="t2", topic="鱼市", channel="ch1")
        assert not is_new
        assert s2.session_id == s1.session_id
        # task_id 更新
        s2_fresh = sm.get(s1.session_id)
        assert s2_fresh.task_id == "t2"

    def test_fuzzy_match_same_channel_topic_contains(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        # 第一次: topic="鱼市砍价" task="t1"
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市砍价", channel="ch1")
        # 第二次: topic="鱼市" (新 task 但 topic 是 s1 的子串)
        s2, is_new = sm.decide_session(task_id="t2", topic="鱼市", channel="ch1")
        assert not is_new
        assert s2.session_id == s1.session_id
        # task_id 应更新
        s2_fresh = sm.get(s1.session_id)
        assert s2_fresh.task_id == "t2"

    def test_fuzzy_match_topic_in_other(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        s2, is_new = sm.decide_session(task_id="t2", topic="鱼市砍价", channel="ch1")
        assert not is_new
        assert s2.session_id == s1.session_id

    def test_different_channel_creates_new(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        s2, is_new = sm.decide_session(task_id="t1", topic="鱼市", channel="ch2")
        assert is_new

    def test_completed_session_not_matched(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        s1, _ = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        sm.update(s1.session_id, status="completed")
        s2, is_new = sm.decide_session(task_id="t1", topic="鱼市", channel="ch1")
        assert is_new  # 不命中已 completed


class TestSessionManagerConcurrency:
    def test_concurrent_writes_no_corruption(self, tmp_path):
        """多线程并发写不应破坏文件."""
        import threading
        sm = SessionManager(tmp_path / "s.json", "a")
        errors = []

        def writer(i):
            try:
                for j in range(20):
                    s = sm.create(topic=f"t{i}_{j}")
                    sm.update(s.session_id, progress=j * 5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()

        assert not errors
        # 文件应可解析
        data = json.loads((tmp_path / "s.json").read_text())
        assert len(data["sessions"]) == 80  # 4 writers * 20 sessions
