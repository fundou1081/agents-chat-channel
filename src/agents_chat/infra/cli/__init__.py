"""
v2 CLI adapters — abstract base + 3 implementations.

| Adapter | 用法 |
|---------|------|
| CLI (base)         | 抽象基类 |
| CLIResponse        | 响应数据类 |
| new_session_id     | session id 工具 |
| MockCLI            | 测试用 (无 LLM 调用) |
| OpenCodeCLI        | OpenCode CLI 适配 |
| QwenCLI            | Qwen CLI 适配 |
"""
from .base import CLI, CLIResponse, new_session_id
from .mock import MockCLI
from .opencode import OpenCodeCLI, _find_cli
from .qwen import QwenCLI

__all__ = [
    "CLI",
    "CLIResponse",
    "MockCLI",
    "OpenCodeCLI",
    "QwenCLI",
    "new_session_id",
    "_find_cli",
]
