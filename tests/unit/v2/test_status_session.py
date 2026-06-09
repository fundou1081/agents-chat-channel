"""Unit tests for v2.0 STATUS parser.

注意: SessionIndex 测试已移除 (2026-06-08 清理) - 该类功能被 SessionManager.remote_id 覆盖.
"""
import pytest

from agents_chat.v2.core.status import (
    Status,
    extract_status_from_message,
    format_status,
    format_status_block,
    parse_status_block,
)


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
        """单行 format_status 只含 summary + next_action (其它字段不落).
        多行 format_status_block 才含所有字段. round-trip 各自验证."""
        s = Status(
            session_id="s1", task_id="t1", progress=80,
            summary="doing x", next_action="do y", confidence="high",
        )
        # 单行: 只含 summary + next_action
        text = format_status(s)
        s2 = parse_status_block(text)
        assert s2.summary == s.summary
        assert s2.next_action == s.next_action
        # session_id / task_id / progress / confidence 单行不含 (LLM 输不出来)

        # 多行: 包含所有字段
        text2 = format_status_block(s)
        s3 = parse_status_block(text2)
        assert s3.session_id == s.session_id
        assert s3.task_id == s.task_id
        assert s3.progress == s.progress
        assert s3.confidence == s.confidence


