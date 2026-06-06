"""
STATUS block parser for v2.0.

Every agent reply MUST embed a <!--STATUS ... --> block for observability.
Scanner 解析并更新 state_board.

格式 (v2.0 设计文档 9.1):
<!--STATUS
 session_id: local_sess_001
 task_id: task_042
 progress: 70
 summary: 已定位数据库连接池耗尽, 正分析连接泄漏点.
 next_action: 审计最近上线的服务代码, 预计30分钟完成.
 confidence: high
-->

字段值均为纯文本, progress 0-100 整数, confidence low/medium/high.
块可嵌回复任意位置, 正则提取 (取最后一个, 因为可能有多个, 取最新).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional


# 外层块: <!--STATUS ... -->
_BLOCK_RE = re.compile(r"<!--STATUS\s*\n(.*?)\n-->", re.DOTALL)
# 字段行: key: value
_FIELD_RE = re.compile(r"^\s*(\w+)\s*:\s*(.+?)\s*$", re.MULTILINE)

VALID_CONFIDENCE = ("low", "medium", "high")


@dataclass
class Status:
    """解析后的状态报告."""
    session_id: str = ""
    task_id: str = ""
    progress: int = 0           # 0-100
    summary: str = ""
    next_action: str = ""
    confidence: str = "medium"  # low/medium/high
    raw: str = ""               # 原始 block 文本 (debug)

    def to_dict(self) -> dict:
        return asdict(self)

    def is_valid(self) -> bool:
        """最少要有 task_id 和 progress (调度中心靠这两个做决策)."""
        return bool(self.task_id) and 0 <= self.progress <= 100


def parse_status_block(text: str) -> Optional[Status]:
    """从一段文本中提取 STATUS 块. 返回 Status 或 None.

    多个块时取最后一个 (最新的).
    """
    if not text:
        return None
    blocks = _BLOCK_RE.findall(text)
    if not blocks:
        return None
    raw = blocks[-1]
    fields = dict(_FIELD_RE.findall(raw))
    if not fields:
        return None

    # progress 解析 (允许 "70" 或 "70%" 或 "70/100")
    progress = 0
    if "progress" in fields:
        m = re.match(r"(\d+)", fields["progress"])
        if m:
            progress = max(0, min(100, int(m.group(1))))

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


def extract_status_from_message(content: str) -> Optional[Status]:
    """从频道消息 content 中提取 STATUS (parse_status_block 的 alias)."""
    return parse_status_block(content)


def format_status(status: Status) -> str:
    """生成 STATUS 块字符串 (agent 输出时用)."""
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
