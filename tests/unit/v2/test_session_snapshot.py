"""Tests for SessionSnapshot + decide_session 接受 session_snapshot 上下文."""
import pytest

from agents_chat.v2.session_manager import Session, SessionManager, SessionSnapshot


class TestSessionSnapshot:
    def test_snapshot_fields(self):
        """Session.snapshot() 返回轻量 SessionSnapshot."""
        s = Session(
            session_id="s1", topic="鱼市砍价", progress=50,
            next_action="等 buyer", content_summary="开价 100",
            status="active", task_id="t1", channel="fish-market",
            remote_id="qwen_xxx",  # 不应出现在 snapshot
        )
        snap = s.snapshot()
        assert isinstance(snap, SessionSnapshot)
        assert snap.session_id == "s1"
        assert snap.topic == "鱼市砍价"
        assert snap.progress == 50
        assert snap.next_action == "等 buyer"
        assert snap.content_summary == "开价 100"
        assert snap.status == "active"
        assert snap.task_id == "t1"
        assert snap.channel == "fish-market"
        # Session 独有字段不应出现
        assert not hasattr(snap, "remote_id")
        assert not hasattr(snap, "last_active")

    def test_snapshot_to_dict(self):
        s = Session(session_id="s1", topic="x")
        snap = s.snapshot()
        d = snap.to_dict()
        assert d["session_id"] == "s1"
        assert d["topic"] == "x"
        assert "remote_id" not in d


class TestSessionManagerSnapshot:
    def test_snapshot_returns_list(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        sm.create(topic="a")
        sm.create(topic="b")
        snaps = sm.snapshot()
        assert len(snaps) == 2
        for snap in snaps:
            assert isinstance(snap, SessionSnapshot)

    def test_snapshot_empty(self, tmp_path):
        sm = SessionManager(tmp_path / "s.json", "a")
        assert sm.snapshot() == []


class TestDecideSessionWithSnapshot:
    """decide_session 接受 session_snapshot 参数, 用于更智能判断."""

    def test_progress_100_does_not_continue(self, tmp_path):
        """如果已有 session progress=100, 不续 (避免重复触发)."""
        sm = SessionManager(tmp_path / "s.json", "a")
        # 已有 progress=100 的 session
        s = sm.create(topic="鱼市", channel="fish-market", task_id="t_old")
        sm.update(s.session_id, progress=100, status="completed")
        # 新 task, 主题相同 (本应 fuzzy 命中)
        s_new, is_new = sm.decide_session(
            task_id="t_new", topic="鱼市", channel="fish-market",
        )
        # 应该不续 (因为 progress=100), 建新的
        assert is_new
        assert s_new.session_id != s.session_id

    def test_progress_50_continues(self, tmp_path):
        """progress<100 的 session 应该续."""
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="鱼市", channel="fish-market", task_id="t_old")
        sm.update(s.session_id, progress=50)  # active
        s_new, is_new = sm.decide_session(
            task_id="t_new", topic="鱼市", channel="fish-market",
        )
        assert not is_new
        assert s_new.session_id == s.session_id

    def test_no_snapshot_default_continues(self, tmp_path):
        """没传 session_snapshot (默认 None) → 老的 fuzzy 续行为."""
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="鱼市", channel="fish-market", task_id="t_old")
        sm.update(s.session_id, progress=100)  # progress=100
        s_new, is_new = sm.decide_session(
            task_id="t_new", topic="鱼市", channel="fish-market",
            # 不传 session_snapshot
        )
        # 默认 None, 老的 fuzzy 命中 (会续)
        assert not is_new

    def test_explicit_snapshot_param(self, tmp_path):
        """显式传 SessionSnapshot 也能用."""
        from agents_chat.v2.session_manager import SessionSnapshot
        sm = SessionManager(tmp_path / "s.json", "a")
        s = sm.create(topic="鱼市", channel="fish-market", task_id="t_old")
        sm.update(s.session_id, progress=100)
        # 显式 snapshot
        snap = SessionSnapshot(
            session_id="s_other", topic="其他", progress=0,
            next_action="", content_summary="", status="active",
            task_id="t_other", channel="other",
        )
        s_new, is_new = sm.decide_session(
            task_id="t_new", topic="鱼市", channel="fish-market",
            session_snapshot=snap,
        )
        # snap 是另一个 session, 不影响 decide
        # 已有 session progress=100, 不续
        assert is_new
