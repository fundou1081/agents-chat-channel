"""
CLI base for v2.0.

每个 Agent 绑定一个外部智能体 CLI 程序 (qwen / claude / opencode / mock).
CLI 抽象:
  - name: 标识 (e.g. "qwen", "opencode", "mock")
  - async invoke(prompt, resume_session=None) -> CLIResponse
    - 输入: prompt 文本 + 可选 resume session_id
    - 输出: CLIResponse {output_text, new_session_id, raw}

v2.0 设计文档假设 CLI 有 --resume 参数. 实际上:
  - opencode: 有 (subprocess 调用 opencode resume --session xxx)
  - qwen HTTP API: 无 (需要本地 history 模拟, 见 qwen.py)
  - mock: 无 (固定 session_id, 便于测试)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class CLIResponse:
    """CLI 调用结果."""
    output_text: str
    new_session_id: Optional[str] = None  # 第一次调用时由 CLI 返回, resume 时不返回
    raw: str = ""                         # 原始 stdout (debug)
    error: str = ""                        # 错误信息
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


@runtime_checkable
class CLI(Protocol):
    """CLI 抽象 (Protocol, 不需要显式继承)."""

    name: str

    async def invoke(
        self, prompt: str, resume_session: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> CLIResponse:
        """调用 CLI. resume_session=None 时创建新 session, 否则恢复.

        workspace_dir: Agent 的工作目录.
          - opencode / claude: subprocess(cwd=workspace_dir), CLI 启动后读 claude.md / opencode.md
          - qwen (HTTP): 不用 cwd, 但可能在 prompt 里说 "看 qwen.md"
          - mock: 忽略
        """
        ...


def new_session_id(prefix: str = "sess") -> str:
    """生成新 session id (helper)."""
    return f"{prefix}_{uuid.uuid4().hex[:10]}"
