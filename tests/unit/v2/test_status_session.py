"""Unit tests for v2.0 STATUS parser + SessionIndex."""
import pytest

from agents_chat.v2.status import (
    Status,
    extract_status_from_message,
    format_status,
    parse_status_block,
)
from agents_chat.v2.session_index import SessionIndex


class TestStatusParse:
    def test_basic_parse(self):
        text = """已完成任务
<!--STATUS
 session_id: local_qwen_001
 task_id: task_042
 progress: 70
 summary: 已定位连接池耗尽
 next_action: 审计服务代码
 confidence: high
-->
"""
        s = parse_status_block(text)
        assert s is not None
        assert s.session_id == "local_qwen_001"
        assert s.task_id == "task_042"
        assert s.progress == 70
        assert s.confidence == "high"
        assert "连接池" in s.summary
        assert s.is_valid()

    def test_missing_block(self):
        assert parse_status_block("plain text") is None
        assert parse_status_block("") is None

    def test_take_last_block(self):
        """多个 STATUS 块时取最后一个 (最新)."""
        text = """<!--STATUS
 task_id: a
 progress: 10
-->
中间文字
<!--STATUS
 task_id: b
 progress: 50
-->
"""
        s = parse_status_block(text)
        assert s.task_id == "b"
        assert s.progress == 50

    def test_progress_with_percent(self):
        text = """<!--STATUS
 task_id: t1
 progress: 75%
-->"""
        s = parse_status_block(text)
        assert s.progress == 75

    def test_progress_clamp(self):
        text = """<!--STATUS
 task_id: t1
 progress: 150
-->"""
        s = parse_status_block(text)
        assert s.progress == 100  # clamp

        text = """<!--STATUS
 task_id: t1
 progress: -5
-->"""
        s = parse_status_block(text)
        assert s.progress == 0

    def test_invalid_confidence_defaults_medium(self):
        text = """<!--STATUS
 task_id: t1
 progress: 50
 confidence: 非常高
-->"""
        s = parse_status_block(text)
        assert s.confidence == "medium"

    def test_minimal_valid(self):
        """只有 task_id + progress 也算 valid."""
        text = """<!--STATUS
 task_id: t1
 progress: 0
-->"""
        s = parse_status_block(text)
        assert s.is_valid()
        assert s.summary == ""

    def test_invalid_no_task_id(self):
        text = """<!--STATUS
 progress: 50
-->"""
        s = parse_status_block(text)
        assert not s.is_valid()

    def test_chinese_content(self):
        text = """数据库连接池泄漏分析
<!--STATUS
 session_id: sess_001
 task_id: 数据库任务
 progress: 50
 summary: 已定位连接泄漏点, 在 service/auth.go:142
 next_action: 修复后回归测试
 confidence: high
-->"""
        s = parse_status_block(text)
        assert s.task_id == "数据库任务"
        assert "service/auth.go" in s.summary
        assert s.is_valid()

    def test_format_round_trip(self):
        s = Status(
            session_id="s1", task_id="t1", progress=80,
            summary="doing x", next_action="do y", confidence="high",
        )
        text = format_status(s)
        s2 = parse_status_block(text)
        assert s2.session_id == s.session_id
        assert s2.task_id == s.task_id
        assert s2.progress == s.progress
        assert s2.confidence == s.confidence


class TestSessionIndex:
    def test_create_and_get(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        local_id = idx.create(topic="数据库", channel="general", remote_id="qwen_sess_42")
        sess = idx.get(local_id)
        assert sess["remote_session_id"] == "qwen_sess_42"
        assert sess["topic"] == "数据库"
        assert sess["channel"] == "general"

    def test_get_remote(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        local_id = idx.create(remote_id="qwen_sess_99")
        assert idx.get_remote(local_id) == "qwen_sess_99"

    def test_set_remote_updates(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        local_id = idx.create()  # remote_id empty
        assert idx.get_remote(local_id) is None
        idx.set_remote(local_id, "qwen_sess_42")
        assert idx.get_remote(local_id) == "qwen_sess_42"

    def test_find_by_topic(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        idx.create(topic="数据库连接池", channel="general")
        idx.create(topic="安全审计", channel="general")
        # 模糊匹配
        assert idx.find_by_topic("数据库") is not None
        assert idx.find_by_topic("安全") is not None
        assert idx.find_by_topic("不存在的") is None

    def test_touch_updates_last_active(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        local_id = idx.create()
        before = idx.get(local_id)["last_active"]
        import time; time.sleep(0.01)
        idx.touch(local_id)
        after = idx.get(local_id)["last_active"]
        assert after >= before

    def test_remove(self, tmp_path):
        idx = SessionIndex(tmp_path / "qwencode.json", "qwencode")
        local_id = idx.create()
        assert idx.remove(local_id) is True
        assert idx.get(local_id) is None

    def test_persistence(self, tmp_path):
        """文件持久化: 重建 SessionIndex 应该读到数据."""
        p = tmp_path / "qwencode.json"
        idx1 = SessionIndex(p, "qwencode")
        local_id = idx1.create(topic="x", remote_id="qwen_sess_42")
        # 重新构造 (模拟重启)
        idx2 = SessionIndex(p, "qwencode")
        sess = idx2.get(local_id)
        assert sess is not None
        assert sess["remote_session_id"] == "qwen_sess_42"
