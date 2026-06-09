"""
v2 Infra: 基础设施 (I/O + 适配器 + 入口).

| Module | 职责 |
|--------|------|
| main             | CLI 入口 (init/run-worker/post/status/tail/inbox/reset) |
| server           | FastAPI HTTP server (WebUI 静态文件 + REST API) |
| worker_factory   | Worker 工厂 (CLI 适配 + PDR 组装) |
| gates            | 输入/输出 Gate 链 (过滤 + 安全) |
| state_board      | 全局状态板 (跨频道任务跟踪) |
| files.channel    | Channel 文件 I/O (频道消息 jsonl) |
| files.mailbox    | Mailbox 文件 I/O (agent 邮箱) |
| files.lock       | 文件锁 (acquire/release/refresh/is_expired/...) |
| cli.base         | CLI 抽象基类 |
| cli.mock         | Mock CLI (测试用) |
| cli.opencode     | OpenCode CLI 适配 |
| cli.qwen         | Qwen CLI 适配 |
"""
from .files.channel import Channel, fuzzy_resolve_mention
from .files.mailbox import Mailbox
from .gates import (
    Gate,
    GateChain,
    GateResult,
    MaxLengthGate,
    SecretLeakGate,
)
from .state_board import StateBoard
from .worker_factory import WorkerFactory, register_cli

__all__ = [
    # files
    "Channel",
    "Mailbox",
    "fuzzy_resolve_mention",
    # gates
    "Gate",
    "GateChain",
    "GateResult",
    "MaxLengthGate",
    "SecretLeakGate",
    # state
    "StateBoard",
    # factory
    "WorkerFactory",
    "register_cli",
]
