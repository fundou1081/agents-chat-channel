"""
Stage 完成检查 — v2 checks 启发式 + 高级 type.

设计文档: docs/26-stage-workflow.md 章节 4.3 + 6.1

checks 表达:
  - 简单: 字符串列表 (90% 场景)
    启发式: 含 markdown/html 标记 → contains (scheduler 校验)
            其他 → hint (只给 worker 看)
  - 高级: dict 列表 (10% 场景, 显式 type)
    type: hint | contains | contains_any | contains_all | min_keywords | regex

evaluate_checks(checks, content) -> CheckResult
"""
from __future__ import annotations

import re
from typing import Any

from .schema import CheckItem, CheckResult


# 启发式: 看起来像 "must contain" 的字符串特征
_SUBSTRING_MARKERS = (
    "## ",    # markdown h2
    "# ",     # markdown h1
    "**",     # bold
    "_",      # italic
    "`",      # code
    "<",      # html tag
    "---",    # hr / frontmatter
    '"',      # JSON
    "{",      # JSON object
    "[",      # markdown link
)


def _is_substring_check(text: str) -> bool:
    """启发式: 字符串看起来像 'must contain' 还是 'hint'.

    规则:
      - 含 markdown/html/JSON 标记 → contains (scheduler 校验)
      - 像文件路径 (.json/.md/.txt/.html) → contains
      - 否则 hint (只 log, 不校验)
    """
    if any(marker in text for marker in _SUBSTRING_MARKERS):
        return True
    # 文件路径 (含 .json / .md / .txt / .html)
    if re.search(r"\.[a-z]{2,4}$", text.strip()):
        return True
    return False


def _normalize_check(raw: str | dict) -> tuple[str, str, Any]:
    """把 check item 标准化为 (type, value, raw) 三元组.

    字符串 → 启发式分类
    dict → 读 type 字段
    """
    if isinstance(raw, str):
        if _is_substring_check(raw):
            return "contains", raw, raw
        else:
            return "hint", raw, raw
    elif isinstance(raw, dict):
        t = raw.get("type", "hint")
        if t == "contains":
            return "contains", raw.get("value", ""), raw
        elif t == "contains_any":
            return "contains_any", raw.get("values", []), raw
        elif t == "contains_all":
            return "contains_all", raw.get("values", []), raw
        elif t == "min_keywords":
            return "min_keywords", (raw.get("count", 0), raw.get("keywords", [])), raw
        elif t == "regex":
            return "regex", raw.get("pattern", ""), raw
        elif t == "hint":
            return "hint", raw.get("value", ""), raw
        else:
            # 未知 type, 当 hint 处理
            return "hint", str(raw), raw
    else:
        # 未知类型, 当 hint
        return "hint", str(raw), raw


def _execute_check(check_type: str, value: Any, content: str) -> CheckItem:
    """执行单条 check, 返 CheckItem."""
    if check_type == "hint":
        # hint 永远 pass, 不校验
        return CheckItem(
            raw=value,
            type="hint",
            passed=True,
            detail=f"hint (提示给 worker, 不校验): {value!r}",
            value=value,
        )

    elif check_type == "contains":
        passed = value in content
        return CheckItem(
            raw=value,
            type="contains",
            passed=passed,
            detail=f"expected substring: {value!r} (found: {passed})",
            value=value,
        )

    elif check_type == "contains_any":
        # value 是 list, 任一子串匹配即过
        if not value:
            return CheckItem(
                raw=value, type="contains_any", passed=True,
                detail="empty values list (skip)", value=value,
            )
        found = [v for v in value if v in content]
        passed = bool(found)
        return CheckItem(
            raw=value, type="contains_any", passed=passed,
            detail=f"matched {len(found)}/{len(value)}: {found[:3]}{'...' if len(found) > 3 else ''}",
            value=value,
        )

    elif check_type == "contains_all":
        # value 是 list, 全部子串必须匹配
        missing = [v for v in value if v not in content]
        passed = not missing
        return CheckItem(
            raw=value, type="contains_all", passed=passed,
            detail=f"missing {len(missing)}/{len(value)}: {missing[:3]}{'...' if len(missing) > 3 else ''}",
            value=value,
        )

    elif check_type == "min_keywords":
        # value 是 (count, keywords) tuple
        count, keywords = value
        if not keywords:
            return CheckItem(
                raw=value, type="min_keywords", passed=True,
                detail="empty keywords list (skip)", value=value,
            )
        found_count = sum(1 for k in keywords if k in content)
        passed = found_count >= count
        return CheckItem(
            raw=value, type="min_keywords", passed=passed,
            detail=f"found {found_count}/{count} required (keywords: {keywords})",
            value=value,
        )

    elif check_type == "regex":
        try:
            matched = re.search(value, content, re.DOTALL) is not None
            return CheckItem(
                raw=value, type="regex", passed=matched,
                detail=f"regex match: {value!r}",
                value=value,
            )
        except re.error as e:
            return CheckItem(
                raw=value, type="regex", passed=False,
                detail=f"invalid regex: {e}",
                value=value,
            )

    else:
        # 未知 type, 当 hint
        return CheckItem(
            raw=value, type="hint", passed=True,
            detail=f"unknown check type (treated as hint): {check_type}",
            value=value,
        )


def evaluate_checks(
    checks: list[str | dict],
    content: str,
) -> CheckResult:
    """执行一组 check, 返每个的 pass/fail + 整体结果.

    Args:
        checks: 来自 DeliverableSpec.checks (混合字符串 + dict)
        content: deliverable 文件内容 (文本)

    Returns:
        CheckResult: 包含每个 check 的 CheckItem + 整体 all_passed

    Examples:
        >>> result = evaluate_checks(
        ...     ["## 结论", "至少 3 个来源"],
        ...     "## 结论\\n本文研究了..."
        ... )
        >>> result.all_passed
        True  # "## 结论" 找到 (contains), "至少 3 个来源" 当 hint (不校验)
    """
    items: list[CheckItem] = []
    for raw in checks:
        check_type, value, _ = _normalize_check(raw)
        item = _execute_check(check_type, value, content)
        items.append(item)
    all_passed = all(i.passed for i in items)
    return CheckResult(items=items, all_passed=all_passed)
