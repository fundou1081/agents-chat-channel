"""
STATUS block parser for v2.0.

Every agent reply SHOULD embed a STATUS 块 for observability. Scanner 解析用于调度.

支持 2 种格式 (按优先级尝试):

  1. **单行格式** (对齐 Claude Code AGENTS.md 风格, 图 5):
     [STATUS] 已定位连接池耗尽 | 下一步: 审计服务代码
     或
     [STATUS] progress=70 confidence=high | 已定位 | 下一步: 审计

  2. **多行 HTML 注释** (v2.0 原生, 向后兼容):
     <!--STATUS
      session_id: local_sess_001
      task_id: task_042
      progress: 70
      summary: 已定位连接池耗尽
      next_action: 审计服务代码
      confidence: high
     -->

优先尝试单行 (LLM 容易生成), 失败 fallback 多行. 多个块时取最后一个 (最新).

设计文档参考: v2.0 设计文档 9.1 (多行) + Claude Code AGENTS.md 经验 (单行).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional


# 多行块: <!--STATUS ... -->
_BLOCK_RE = re.compile(r"<!--STATUS\s*\n(.*?)\n-->", re.DOTALL)
# 字段行: key: value
_FIELD_RE = re.compile(r"^\s*(\w+)\s*:\s*(.+?)\s*$", re.MULTILINE)


def _parse_single_line(text: str) -> Optional["Status"]:
    """解析单行 [STATUS] x | y | z 格式 (Claude Code 风格).

    多种变体支持:
      [STATUS] 已完成 | 下一步: 提交
      [STATUS] progress=70 confidence=high | 已完成 | 下一步: 提交
      [STATUS] summary: 已完成 | next_action: 提交
      [STATUS] 任务: task_001 | 进度: 50 | 下一步: 提交
    """
    lines = text.split("\n")
    status_lines = [l.strip() for l in lines if l.strip().startswith("[STATUS]")]
    if not status_lines:
        return None
    last = status_lines[-1]
    body = last[len("[STATUS]"):].strip()
    if not body:
        return None
    parts = [p.strip() for p in body.split("|")]
    if not parts:
        return None

    fields: dict[str, str] = {}
    summary_parts: list[str] = []
    next_action = ""

    for p in parts:
        # 试 part 内多个 kv (e.g. "progress=70 confidence=high")
        sub_kvs = re.findall(r"(\w+)\s*[=:]\s*([^\s|=]+?)(?=\s+\w+\s*[=:]|\s*$)", p.strip())
        if sub_kvs:
            for key, val in sub_kvs:
                key = key.lower()
                val = val.strip()
                if key in ("下一步", "next", "nextaction", "next_action"):
                    key = "next_action"
                elif key in ("summary", "summ", "s"):
                    key = "summary"
                elif key in ("progress", "p", "进度"):
                    key = "progress"
                elif key in ("confidence", "c", "conf"):
                    key = "confidence"
                elif key in ("task", "taskid", "task_id", "任务"):
                    key = "task_id"
                elif key in ("session", "sessionid", "session_id"):
                    key = "session_id"
                fields[key] = val
            continue
        # 单个 kv
        kv_match = re.match(r"^(\w+)\s*[=:]\s*(.+)$", p)
        if kv_match:
            key = kv_match.group(1).lower()
            val = kv_match.group(2).strip()
            if key in ("下一步", "next", "nextaction", "next_action"):
                key = "next_action"
            elif key in ("summary", "summ", "s"):
                key = "summary"
            elif key in ("progress", "p", "进度"):
                key = "progress"
            elif key in ("confidence", "c", "conf"):
                key = "confidence"
            elif key in ("task", "taskid", "task_id", "任务"):
                key = "task_id"
            elif key in ("session", "sessionid", "session_id"):
                key = "session_id"
            fields[key] = val
        else:
            # "下一步: xxx" 格式
            next_m = re.match(
                r"^(?:下一步|next(?:\s*action)?)[:\s]+(.+)$", p, re.IGNORECASE
            )
            if next_m:
                next_action = next_m.group(1).strip()
            else:
                summary_parts.append(p)

    if summary_parts and "summary" not in fields:
        fields["summary"] = " | ".join(summary_parts)
    if next_action and "next_action" not in fields:
        fields["next_action"] = next_action

    if not fields or ("summary" not in fields and "next_action" not in fields):
        return None

    # progress 解析
    progress = 0
    if "progress" in fields:
        m = re.match(r"(\d+)", fields["progress"])
        if m:
            progress = max(0, min(100, int(m.group(1))))

    confidence = fields.get("confidence", "medium").strip().lower()
    VALID_CONFIDENCE = ("low", "medium", "high")
    if confidence not in VALID_CONFIDENCE:
        confidence = "medium"

    return Status(
        session_id=fields.get("session_id", "").strip(),
        task_id=fields.get("task_id", "").strip(),
        progress=progress,
        summary=fields.get("summary", "").strip(),
        next_action=fields.get("next_action", "").strip(),
        confidence=confidence,
        raw=last,
    )


def parse_status_block(text: str) -> Optional["Status"]:
    """从一段文本中提取 STATUS 块. 返回 Status 或 None.

    优先单行格式 (Claude Code 风格), fallback 多行 HTML.
    多个块时取最后一个 (最新的).
    """
    if not text:
        return None
    # 1. 先试单行格式
    single = _parse_single_line(text)
    if single:
        return single
    # 2. Fallback 多行 HTML
    blocks = _BLOCK_RE.findall(text)
    if not blocks:
        return None
    raw = blocks[-1]
    fields = dict(_FIELD_RE.findall(raw))
    if not fields:
        return None
    progress = 0
    if "progress" in fields:
        m = re.match(r"(\d+)", fields["progress"])
        if m:
            progress = max(0, min(100, int(m.group(1))))
    VALID_CONFIDENCE = ("low", "medium", "high")
    confidence = fields.get("confidence", "medium").strip().lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "medium"
    return Status(
        session_id=fields.get("session_id", "").strip(),
        task_id=fields.get("task_id", "").strip(),
        progress=progress,
        summary=fields.get("summary", "").strip(),
        next_action=fields.get("next_action", "").strip(),
        confidence=confidence,
        raw=raw,
    )


def extract_status_from_message(content: str) -> Optional["Status"]:
    """从频道消息 content 中提取 STATUS (parse_status_block 的 alias)."""
    return parse_status_block(content)


def format_status(status: "Status") -> str:
    """生成单行 STATUS 字符串 (agent 输出时用, 对齐 Claude Code 风格)."""
    parts = []
    if status.summary:
        parts.append(status.summary)
    if status.next_action:
        parts.append(f"下一步: {status.next_action}")
    return f"[STATUS] {' | '.join(parts)}" if parts else "[STATUS] (empty)"


def format_status_block(status: "Status") -> str:
    """生成多行 STATUS 块 (HTML 注释格式, 向后兼容)."""
    return (
        "<!--STATUS\n"
        f" session_id: {status.session_id}\n"
        f" task_id: {status.task_id}\n"
        f" progress: {status.progress}\n"
        f" summary: {status.summary}\n"
        f" next_action: {status.next_action}\n"
        f" confidence: {status.confidence}\n"
        "-->"
    )


@dataclass
class Status:
    """解析后的状态报告."""
    session_id: str = ""
    task_id: str = ""
    progress: int = 0
    summary: str = ""
    next_action: str = ""
    confidence: str = "medium"
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def is_valid(self) -> bool:
        """最少要有 task_id 和 progress (调度中心靠这两个做决策)."""
        return bool(self.task_id) and 0 <= self.progress <= 100
