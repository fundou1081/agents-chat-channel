"""
Stage 完成检查 (v2 checks) 测试.

覆盖:
  1. test_string_contains_heuristic - 含 markdown 标记 → contains 校验
  2. test_string_hint_heuristic - 纯文字 → hint (不校验)
  3. test_dict_explicit_types - 6 种 type 显式
  4. test_mixed_string_and_dict - 字符串 + dict 混用
  5. test_check_result_aggregation - 整体 pass/fail
"""
from __future__ import annotations

import pytest

from agents_chat.workflow import evaluate_checks
from agents_chat.workflow.checks import _is_substring_check, _normalize_check


# =============================================================================
# Test 1: 启发式 - contains
# =============================================================================


class TestStringContainsHeuristic:
    def test_markdown_h2_contains(self):
        """含 '## ' 标记 → contains 校验."""
        assert _is_substring_check("## 结论") is True
        assert _is_substring_check("## 来源") is True

    def test_markdown_bold_contains(self):
        """含 '**' 标记 → contains."""
        assert _is_substring_check("**重要**") is True

    def test_html_tag_contains(self):
        """含 '<' 标记 → contains."""
        assert _is_substring_check("<html>") is True
        assert _is_substring_check("<title>") is True

    def test_file_path_contains(self):
        """像文件路径 (含 .json/.md/.txt) → contains."""
        assert _is_substring_check("data/findings.json") is True
        assert _is_substring_check("summary.md") is True
        assert _is_substring_check("report.txt") is True

    def test_json_quote_contains(self):
        """含双引号 → contains (像是 JSON 内容)."""
        assert _is_substring_check('"approved": true') is True

    def test_markdown_link_contains(self):
        """含 '[' 标记 → hint (不是 contains, [ 太泛化)."""
        assert _is_substring_check("[example](url)") is False


# =============================================================================
# Test 2: 启发式 - hint
# =============================================================================


class TestStringHintHeuristic:
    def test_chinese_sentence_hint(self):
        """中文句子 (无 markdown 标记) → hint."""
        assert _is_substring_check("至少 3 个权威来源") is False
        assert _is_substring_check("中文, 2000+ 字") is False
        assert _is_substring_check("包含反方观点") is False

    def test_english_sentence_hint(self):
        """英文句子 → hint."""
        assert _is_substring_check("at least 3 authoritative sources") is False

    def test_short_phrase_hint(self):
        """短词组 (无标记) → hint."""
        assert _is_substring_check("done") is False
        assert _is_substring_check("complete") is False


# =============================================================================
# Test 3: dict 高级 type
# =============================================================================


class TestDictExplicitTypes:
    def test_explicit_hint(self):
        """type: hint 永远 pass."""
        check = {"type": "hint", "value": "至少 3 个来源"}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "hint"
        assert value == "至少 3 个来源"

    def test_explicit_contains(self):
        check = {"type": "contains", "value": "## 结论"}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "contains"
        assert value == "## 结论"

    def test_explicit_contains_any(self):
        check = {"type": "contains_any", "values": ["done", "complete", "finished"]}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "contains_any"
        assert value == ["done", "complete", "finished"]

    def test_explicit_contains_all(self):
        check = {"type": "contains_all", "values": ["## 结论", "## 来源"]}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "contains_all"

    def test_explicit_min_keywords(self):
        check = {"type": "min_keywords", "count": 2, "keywords": ["结论", "建议"]}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "min_keywords"
        assert value == (2, ["结论", "建议"])

    def test_explicit_regex(self):
        check = {"type": "regex", "pattern": "## 结论.*?\\n"}
        ctype, value, _ = _normalize_check(check)
        assert ctype == "regex"


# =============================================================================
# Test 4: evaluate_checks 综合
# =============================================================================


class TestEvaluateChecks:
    def test_all_pass_contains(self):
        """所有 contains check 找到, 全部 pass."""
        content = """# 调研报告
## 结论
本研究显示...
## 来源
1. foo
"""
        result = evaluate_checks(["## 结论", "## 来源"], content)
        assert result.all_passed is True
        assert len(result.items) == 2
        for item in result.items:
            assert item.passed is True
            assert item.type == "contains"

    def test_mixed_contains_and_hint(self):
        """contains + hint 混用, all_passed 看 contains 全部找到."""
        content = "## 结论\n本文..."
        result = evaluate_checks(
            ["## 结论", "至少 3 个来源", "## 来源"],
            content,
        )
        # "## 结论" 找到, "至少 3 个来源" hint pass, "## 来源" 没找到 → fail
        assert result.all_passed is False
        assert len([i for i in result.items if i.passed]) == 2
        assert len([i for i in result.items if not i.passed]) == 1

    def test_contains_any(self):
        """contains_any: 任一子串匹配即过."""
        content = "Status: complete"
        # 多个候选, 至少一个匹配
        result = evaluate_checks(
            [{"type": "contains_any", "values": ["done", "complete", "finished"]}],
            content,
        )
        assert result.all_passed is True
        assert result.items[0].detail.startswith("matched 1/3")

    def test_contains_any_all_missing(self):
        """contains_any 全 missing → fail."""
        content = "Status: in-progress"
        result = evaluate_checks(
            [{"type": "contains_any", "values": ["done", "complete", "finished"]}],
            content,
        )
        assert result.all_passed is False
        assert result.items[0].detail.startswith("matched 0/3")

    def test_contains_all_partial(self):
        """contains_all 部分 missing → fail."""
        content = "## 结论\n..."
        result = evaluate_checks(
            [{"type": "contains_all", "values": ["## 结论", "## 来源"]}],
            content,
        )
        assert result.all_passed is False
        assert "missing 1/2" in result.items[0].detail

    def test_min_keywords_pass(self):
        """min_keywords: 找到 2/2 → pass."""
        content = "本文的结论是... 我们的建议是..."
        result = evaluate_checks(
            [{"type": "min_keywords", "count": 2, "keywords": ["结论", "建议"]}],
            content,
        )
        assert result.all_passed is True
        assert "found 2/2" in result.items[0].detail

    def test_min_keywords_fail(self):
        """min_keywords: 找到 1/2 → fail."""
        content = "本文的结论是..."
        result = evaluate_checks(
            [{"type": "min_keywords", "count": 2, "keywords": ["结论", "建议"]}],
            content,
        )
        assert result.all_passed is False
        assert "found 1/2" in result.items[0].detail

    def test_regex_match(self):
        """regex: 匹配 → pass."""
        content = "## 结论\n本文..."
        result = evaluate_checks(
            [{"type": "regex", "pattern": r"## 结论\w*"}],
            content,
        )
        assert result.all_passed is True

    def test_regex_no_match(self):
        """regex: 不匹配 → fail."""
        content = "本报告没有结论部分"
        result = evaluate_checks(
            [{"type": "regex", "pattern": r"## 结论"}],
            content,
        )
        assert result.all_passed is False

    def test_regex_invalid(self):
        """regex 语法错 → fail (不抛异常)."""
        bad_regex = {"type": "regex", "pattern": r"[unclosed"}  # 错的正则
        result = evaluate_checks([bad_regex], "anything")
        assert result.all_passed is False
        assert "invalid regex" in result.items[0].detail

    def test_mixed_string_and_dict(self):
        """字符串 + dict 混用, 全 work."""
        content = "## 结论\n本文显示... 至少 3 个来源"
        result = evaluate_checks(
            [
                "## 结论",                              # contains
                "至少 3 个来源",                          # hint
                {"type": "contains", "value": "本文显示"},  # contains (dict)
                {"type": "min_keywords", "count": 1, "keywords": ["来源"]},  # min_keywords
            ],
            content,
        )
        assert result.all_passed is True
        assert len(result.items) == 4
        # 第一个 contains, 第二个 hint, 第三个 contains, 第四个 min_keywords
        assert [i.type for i in result.items] == ["contains", "hint", "contains", "min_keywords"]


# =============================================================================
# Test 5: CheckResult 聚合
# =============================================================================


class TestCheckResultAggregation:
    def test_failed_items(self):
        """failed_items() 返失败的 check 列表 (hint 永远 pass 不在里)."""
        result = evaluate_checks(
            ["## 结论", "纯文字 hint", "## 来源", "another hint"],
            "## 结论",  # "## 结论" 找到, "## 来源" 没找到
        )
        # 4 个 check: 2 contains (1 pass, 1 fail), 2 hint (都 pass)
        assert result.all_passed is False
        failed = result.failed_items()
        assert len(failed) == 1
        assert failed[0].type == "contains"
        assert failed[0].raw == "## 来源"

    def test_all_hint_always_pass(self):
        """全是 hint → all_passed True (hint 永远 pass)."""
        result = evaluate_checks(
            ["hint 1", "hint 2", "hint 3"],
            "anything",
        )
        assert result.all_passed is True
        for item in result.items:
            assert item.passed is True
            assert item.type == "hint"

    def test_empty_checks(self):
        """空 checks → all_passed True (无检查项)."""
        result = evaluate_checks([], "anything")
        assert result.all_passed is True
        assert len(result.items) == 0
