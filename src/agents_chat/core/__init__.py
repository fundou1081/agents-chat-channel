"""
v2 Core: PDR (Perceive-Decide-Remember) 核心组件.

| Module | PDR 角色 | 职责 |
|--------|----------|------|
| agent              | Container | 4 组件组装 |
| communication      | Perceive  | 感知 (mailbox + 频道轮询) |
| event_handler      | Decide    | 决策触发器 (passive + proactive) |
| decision           | Decide    | 决策逻辑 (decide_session + decide_speak) |
| session_manager    | Remember  | 记忆 (session 持久化 JSON) |
| status             | -         | status command 解析 |
"""
from .agent import Agent
from .communication import CommunicationComponent
from .decision import Decision, DecisionConfig, DecisionMaker
from .event_handler import EventHandler, extract_mentions
from .session_manager import Session, SessionManager
from .status import Status, parse_status_block

__all__ = [
    "Agent",
    "CommunicationComponent",
    "Decision",
    "DecisionConfig",
    "DecisionMaker",
    "EventHandler",
    "Session",
    "SessionManager",
    "Status",
    "extract_mentions",
    "parse_status_block",
]
