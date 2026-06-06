"""
MockCLI for v2.0 — 简单 echo + 固定响应, 便于 e2e 测试.

行为:
  - invoke(prompt, resume=None): 返回 echo + 固定 reply, new_session_id = mock_<rand>
  - invoke(prompt, resume="mock_xxx"): 同上但 new_session_id = resume (idempotent)
  - 不调任何外部程序, 0 token 消耗

用途:
  - e2e_v2.sh 测试
  - 离线开发
  - 文档演示
"""
from __future__ import annotations

import hashlib
import time
from typing import Optional

from .base import CLIResponse, new_session_id


# 模拟回复模板
_MOCK_REPLY_TMPL = """收到任务: {prompt_summary}

我会处理这个任务, 模拟执行中...

<!--STATUS
 session_id: {session_id}
 task_id: {task_id}
 progress: 100
 summary: MockCLI 完成任务 {task_id}
 next_action: 已完成
 confidence: high
-->"""


def _extract_task_id(prompt: str) -> str:
    """从 prompt 里简单抓 task_id (含 'task_' 字符串)."""
    import re
    m = re.search(r"task[_-](\w+)", prompt, re.IGNORECASE)
    if m:
        return f"task_{m.group(1)}"
    # fallback: 用 prompt 哈希
    return "task_" + hashlib.md5(prompt.encode()).hexdigest()[:8]


class MockCLI:
    name: str = "mock"

    def __init__(self, reply_template: str | None = None):
        self.template = reply_template or _MOCK_REPLY_TMPL
        self.call_count = 0

    async def invoke(
        self, prompt: str, resume_session: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> CLIResponse:
        start = time.time()
        self.call_count += 1

        # 决定 session id: 首次 vs resume
        if resume_session:
            session_id = resume_session
        else:
            session_id = new_session_id("mock")

        # 决定 task id (从 prompt 抓)
        task_id = _extract_task_id(prompt)

        # 截 prompt 摘要 (前 80 字符)
        prompt_summary = prompt.replace("\n", " ").strip()[:80]

        # 生成 reply
        output = self.template.format(
            prompt_summary=prompt_summary,
            session_id=session_id,
            task_id=task_id,
        )

        elapsed = int((time.time() - start) * 1000)
        return CLIResponse(
            output_text=output,
            new_session_id=session_id if not resume_session else None,
            raw=output,
            elapsed_ms=elapsed,
        )
