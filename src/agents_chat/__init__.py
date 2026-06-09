"""
agents-chat-channel v2.0 — Multi-agent runtime with file-bus + PDR architecture.

Subpackages:
  core/   PDR 核心 (agent, communication, event_handler, decision, session_manager, status)
  infra/  基础设施 (main, server, worker_factory, gates, state_board, files/, cli/)
  webui/  静态 WebUI (app.js, index.html, style.css)

公共 API re-export:
  用户可以 `from agents_chat import Agent, Channel, ...`
"""
from __future__ import annotations

# Core PDR
from .core import (
    Agent,
    CommunicationComponent,
    Decision,
    DecisionConfig,
    DecisionMaker,
    EventHandler,
    Session,
    SessionManager,
    Status,
    extract_mentions,
    parse_status_block,
)

# Infra (公共类)
from .infra import (
    Channel,
    Gate,
    GateChain,
    GateResult,
    Mailbox,
    MaxLengthGate,
    SecretLeakGate,
    StateBoard,
    WorkerFactory,
    fuzzy_resolve_mention,
    register_cli,
)

# CLI adapters (for type hints / direct use)
from .infra.cli import CLI, CLIResponse, MockCLI, OpenCodeCLI, QwenCLI

# Workspace path
from pathlib import Path

WEBUI_DIR = Path(__file__).parent / "webui"

__version__ = "2.0.0"

__all__ = [
    # Core
    "Agent", "CommunicationComponent", "Decision", "DecisionConfig",
    "DecisionMaker", "EventHandler", "Session", "SessionManager", "Status",
    "extract_mentions", "parse_status_block",
    # Infra
    "Channel", "Mailbox",
    "Gate", "GateChain", "GateResult", "MaxLengthGate", "SecretLeakGate",
    "StateBoard", "WorkerFactory", "register_cli",
    "fuzzy_resolve_mention",
    # CLI
    "CLI", "CLIResponse", "MockCLI", "OpenCodeCLI", "QwenCLI",
    # Constants
    "WEBUI_DIR", "__version__",
]
