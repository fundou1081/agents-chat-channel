"""Tests for v2.0 single-line STATUS 格式 (Claude Code 风格)."""
import pytest

from agents_chat.v2.core.status import (
    Status,
    format_status,
    format_status_block,
    parse_status_block,
)


class TestSingleLineStatus:
    """单行 [STATUS] 格式 (对齐 Claude Code AGENTS.md 风格)."""

    def test_simple_summary_next(self):
        """[STATUS] summary | 下一步: xxx."""
        text = "我开价 100 元\n[STATUS] 开价 100 | 下一步: 等 buyer"
        s = parse_status_block(text)
        assert s is not None
        assert s.summary == "开价 100"
        assert s.next_action == "等 buyer"

    def test_kv_format(self):
        """[STATUS] progress=70 confidence=high | summary=... | next=..."""
        text = "[STATUS] progress=70 confidence=high | 已完成 | 下一步: 提交"
        s = parse_status_block(text)
        assert s is not None
        assert s.progress == 70
        assert s.confidence == "high"
        assert s.summary == "已完成"
        assert s.next_action == "提交"

    def test_english_keys(self):
        """英文 keys: summary, next_action, task_id."""
        text = "[STATUS] summary: 已完成 | next_action: 提交 | task_id: t1"
        s = parse_status_block(text)
        assert s is not None
        assert s.summary == "已完成"
        assert s.next_action == "提交"
        assert s.task_id == "t1"

    def test_chinese_keys(self):
        """中文 keys: 任务 / 下一步 / 进度."""
        text = "[STATUS] 任务: task_001 | 进度: 50 | 下一步: 提交"
        s = parse_status_block(text)
        assert s is not None
        assert s.task_id == "task_001"
        assert s.progress == 50
        assert s.next_action == "提交"

    def test_only_summary(self):
        text = "处理完了\n[STATUS] 已完成所有任务"
        s = parse_status_block(text)
        assert s is not None
        assert s.summary == "已完成所有任务"
        assert s.next_action == ""

    def test_only_next(self):
        text = "[STATUS] 下一步: 提交 PR"
        s = parse_status_block(text)
        assert s is not None
        assert s.summary == ""
        assert s.next_action == "提交 PR"

    def test_multiple_status_take_last(self):
        text = "[STATUS] 第一轮\n中间内容\n[STATUS] 第二轮"
        s = parse_status_block(text)
        assert s is not None
        assert s.summary == "第二轮"

    def test_progress_with_percent(self):
        text = "[STATUS] progress=75% | 完成 75%"
        s = parse_status_block(text)
        assert s is not None
        assert s.progress == 75

    def test_progress_clamp(self):
        text = "[STATUS] progress=150 | 超了"
        s = parse_status_block(text)
        assert s is not None
        assert s.progress == 100
        text = "[STATUS] progress=-10 | 负的"
        s = parse_status_block(text)
        assert s.progress == 0

    def test_confidence_validation(self):
        text = "[STATUS] confidence=very_high | 错的值"
        s = parse_status_block(text)
        assert s is not None
        assert s.confidence == "medium"  # fallback

    def test_no_status_returns_none(self):
        text = "没有 STATUS 块的普通文本"
        assert parse_status_block(text) is None

    def test_empty_text_returns_none(self):
        assert parse_status_block("") is None


class TestMultiLineStatusStillWorks:
    """多行 HTML 格式 (v2.0 原生) 仍 work (向后兼容)."""

    def test_multiline_basic(self):
        text = """已开价
<!--STATUS
 session_id: s1
 task_id: t1
 progress: 50
 summary: 开价 100
 next_action: 等 buyer
 confidence: high
-->
"""
        s = parse_status_block(text)
        assert s is not None
        assert s.session_id == "s1"
        assert s.task_id == "t1"
        assert s.progress == 50
        assert s.summary == "开价 100"
        assert s.next_action == "等 buyer"
        assert s.confidence == "high"

    def test_multiline_progress_clamp(self):
        text = """<!--STATUS
 progress: 200
 summary: 超了
-->"""
        s = parse_status_block(text)
        assert s.progress == 100

    def test_single_line_takes_priority_over_multiline(self):
        """如果两种格式都出现, 单行优先 (因为单行是最近更新)."""
        text = """<!--STATUS
 progress: 10
 summary: 多行
-->
中间
[STATUS] progress=90 | 单行 | 下一步: 完成"""
        s = parse_status_block(text)
        # 取最后一个 [STATUS], 它是单行
        assert s.progress == 90
        assert s.summary == "单行"


class TestFormatStatus:
    def test_format_status_single_line(self):
        s = Status(session_id="s1", task_id="t1", summary="已完成", next_action="提交", confidence="high", progress=100)
        out = format_status(s)
        assert "[STATUS]" in out
        assert "已完成" in out
        assert "下一步: 提交" in out

    def test_format_status_block_multiline(self):
        s = Status(session_id="s1", task_id="t1", summary="已完成", next_action="提交", confidence="high", progress=100)
        out = format_status_block(s)
        assert "<!--STATUS" in out
        assert "session_id: s1" in out
        assert "summary: 已完成" in out
        assert "-->" in out
