"""Unit tests for v2.0 StateBoard."""
import time
import pytest

from agents_chat.v2.state_board import StateBoard


class TestStateBoard:
    def test_initial_empty(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        assert sb.list_all() == {}

    def test_claim_creates_entry(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        e = sb.claim("task_001", agent_id="qwencode", session_local="local_001",
                      channel="general", ref_msg_id="ch_1")
        assert e["agent"] == "qwencode"
        assert e["progress"] == 0
        assert e["channel"] == "general"
        assert sb.get("task_001") is not None

    def test_update_from_status(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("task_001", "qwencode", "local_001")
        sb.update_from_status("task_001", {
            "progress": 50,
            "summary": "halfway",
            "next_action": "continue",
            "confidence": "high",
        })
        e = sb.get("task_001")
        assert e["progress"] == 50
        assert e["summary"] == "halfway"
        assert e["confidence"] == "high"
        assert e["heartbeat"]  # 更新过

    def test_update_from_status_preserves_metadata(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("task_001", "qwencode", "local_001", channel="general", ref_msg_id="ch_1")
        sb.update_from_status("task_001", {"progress": 50, "summary": "x"})
        e = sb.get("task_001")
        # 保留字段
        assert e["agent"] == "qwencode"
        assert e["session"] == "local_001"
        assert e["channel"] == "general"
        assert e["ref_msg_id"] == "ch_1"

    def test_update_unknown_creates_entry(self, tmp_path):
        """Scanner 兜底: 没有 claim 过但收到 STATUS 也建 entry."""
        sb = StateBoard(tmp_path / "board.json")
        sb.update_from_status("task_orphan", {"progress": 30, "summary": "x"}, agent_id="qwencode")
        e = sb.get("task_orphan")
        assert e is not None
        assert e["agent"] == "qwencode"

    def test_list_by_agent(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("t1", "qwencode", "s1")
        sb.claim("t2", "claude", "s1")
        sb.claim("t3", "qwencode", "s1")
        qwen_tasks = sb.list_by_agent("qwencode")
        assert set(qwen_tasks.keys()) == {"t1", "t3"}

    def test_list_stale(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("t1", "qwencode", "s1")
        sb.claim("t2", "qwencode", "s1")
        # t1 的 heartbeat 改成旧的
        e = sb.get("t1")
        e["heartbeat"] = "2020-01-01T00:00:00+00:00"
        # 写回 (直接读 + 改 + 写)
        import json
        all_data = sb.list_all()
        all_data["t1"]["heartbeat"] = "2020-01-01T00:00:00+00:00"
        # 通过 release + 重建
        # 简单做法: claim 一下
        # 但 claim 不覆盖已存在的, 让我直接用 update_from_status
        sb.update_from_status("t1", {"progress": 10}, agent_id="qwencode")
        # t1 heartbeat 是 now, t2 heartbeat 也是 now. 都不是 stale
        # 让我把 t1 改回 stale:
        all_tasks = sb.list_all()
        all_tasks["t1"]["heartbeat"] = "2020-01-01T00:00:00+00:00"
        # 直接操作文件
        import json as _json
        (tmp_path / "board.json").write_text(_json.dumps(all_tasks))
        # 不行, sb 内存 cache. 重新构造:
        sb2 = StateBoard(tmp_path / "board.json")
        stale = sb2.list_stale(ttl_seconds=60)
        assert "t1" in stale
        assert "t2" not in stale

    def test_touch_heartbeat(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("t1", "qwencode", "s1")
        before = sb.get("t1")["heartbeat"]
        time.sleep(0.01)
        sb.touch_heartbeat("t1")
        after = sb.get("t1")["heartbeat"]
        assert after > before

    def test_release_removes(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("t1", "qwencode", "s1")
        assert sb.release("t1") is True
        assert sb.get("t1") is None

    def test_complete_marks_100(self, tmp_path):
        sb = StateBoard(tmp_path / "board.json")
        sb.claim("t1", "qwencode", "s1")
        sb.complete("t1")
        e = sb.get("t1")
        assert e["progress"] == 100
        assert "completed_at" in e

    def test_persistence(self, tmp_path):
        p = tmp_path / "board.json"
        sb1 = StateBoard(p)
        sb1.claim("t1", "qwencode", "s1")
        sb2 = StateBoard(p)  # 重启
        e = sb2.get("t1")
        assert e is not None
        assert e["agent"] == "qwencode"
