"""
WorkerFactory for v2.0 — CLI 注册表 + Worker 工厂.

统一创建 Worker (Agent 实例), 支持多种 CLI 后端:
  - opencode: OpenCodeCLI (opencode CLI + minimax/deepseek)
  - qwen: QwenCLI (Qwen HTTP API)
  - claude: ClaudeCLI (Claude Code CLI)  [待实现]
  - mock: MockCLI (测试用)

用法:
    from agents_chat.v2.worker_factory import WorkerFactory, register_cli

    # 注册 CLI 适配器 (可选, 默认已注册)
    register_cli("opencode", OpenCodeCLI)
    register_cli("qwen", QwenCLI)
    register_cli("mock", MockCLI)

    # 工厂创建 worker
    worker = WorkerFactory.create(
        agent_id="seller-fish",
        cli_type="opencode",
        data_dir=Path("./data_v2"),
        mode="proactive",
        subscriptions=["fish-market"],
        system_prompt="你是卖鱼小贩...",
    )

    # 从配置 dict 创建 (用于 Server API)
    workers = WorkerFactory.create_all({
        "seller-fish": {"cli": "opencode", "subscriptions": ["fish-market"]},
        "buyer-fish": {"cli": "opencode", "subscriptions": ["fish-market"]},
    }, data_dir=Path("./data_v2"))
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Type

from .cli.base import CLI, CLIResponse
from .cli.mock import MockCLI
from .cli.opencode import OpenCodeCLI
from .cli.qwen import QwenCLI

logger = logging.getLogger(__name__)

# =============================================================================
# CLI Registry
# =============================================================================


class _CLIRegistry:
    """CLI 适配器注册表."""

    def __init__(self):
        self._clis: dict[str, Type[CLI]] = {}

    def register(self, name: str, cli_class: Type[CLI]) -> None:
        """注册一个 CLI 适配器.

        Args:
            name: CLI 名称, 如 "opencode" / "qwen" / "claude" / "mock"
            cli_class: CLI 子类, 如 OpenCodeCLI / QwenCLI
        """
        self._clis[name] = cli_class
        logger.debug(f"Registered CLI: {name} -> {cli_class.__name__}")

    def get(self, name: str) -> Type[CLI] | None:
        """按名称查 CLI 类."""
        return self._clis.get(name)

    def list_names(self) -> list[str]:
        """列出所有注册的 CLI 名称."""
        return list(self._clis.keys())

    def is_registered(self, name: str) -> bool:
        return name in self._clis


# 全局注册表 (单例)
_registry = _CLIRegistry()


def register_cli(name: str, cli_class: Type[CLI]) -> None:
    """注册 CLI 适配器到全局注册表."""
    _registry.register(name, cli_class)


def list_clis() -> list[str]:
    """列出所有已注册的 CLI 名称."""
    return _registry.list_names()


def get_cli_class(name: str) -> Type[CLI] | None:
    """按名称获取 CLI 类."""
    return _registry.get(name)


# =============================================================================
# 默认注册 (opencode / qwen / mock)
# =============================================================================

register_cli("opencode", OpenCodeCLI)
register_cli("qwen", QwenCLI)
register_cli("mock", MockCLI)
# claude: 待实现 (ClaudeCLI)


# =============================================================================
# WorkerFactory
# =============================================================================


class WorkerFactory:
    """Worker (Agent) 工厂.

    统一创建 Agent 实例, 支持从配置 dict 批量创建.
    """

    @staticmethod
    def create(
        agent_id: str,
        cli_type: str,
        data_dir: Path | str,
        *,
        mode: str = "passive",
        subscriptions: Optional[list[str]] = None,
        system_prompt: str = "",
        workspace_dir: Path | str | None = None,
        cli_config: Optional[dict] = None,
        decision_config: Optional[dict] = None,
        input_gates: Optional[list] = None,
        output_gates: Optional[list] = None,
        **kwargs,
    ):
        """创建一个 Worker (Agent 实例).

        Args:
            agent_id: Worker ID (必须全局唯一)
            cli_type: CLI 类型 ("opencode" / "qwen" / "mock" / "claude")
            data_dir: 数据目录
            mode: 运行模式 ("passive" / "proactive")
            subscriptions: 主动模式订阅的频道列表
            system_prompt: Worker 角色提示
            workspace_dir: Worker 独立工作目录 (默认 data_dir/workspaces/{agent_id})
            cli_config: 传给 CLI 构造器的额外配置 (如 model, timeout_seconds)
            decision_config: DecisionMaker 配置 dict
            input_gates / output_gates: Gate 列表
            **kwargs: 其他传给 Agent.__init__ 的参数

        Returns:
            Agent 实例

        Raises:
            ValueError: cli_type 未注册
        """
        cli_class = get_cli_class(cli_type)
        if cli_class is None:
            raise ValueError(
                f"Unknown CLI type '{cli_type}'. "
                f"Available: {list_clis()}. "
                f"Register with register_cli('{cli_type}', YourCLIClass)"
            )

        data_dir = Path(data_dir)
        workspace_dir = Path(workspace_dir) if workspace_dir else data_dir / "workspaces" / agent_id

        # 构造 CLI 实例
        cli_extra = cli_config or {}
        if cli_type == "mock":
            cli = cli_class()  # MockCLI 无参数
        elif cli_type == "opencode":
            cli = cli_class(
                model=cli_extra.get("model", "opencode/deepseek-v4-flash-free"),
                timeout_seconds=cli_extra.get("timeout_seconds", 300),
                binary=cli_extra.get("binary", "opencode"),
            )
        elif cli_type == "qwen":
            cli = cli_class(
                model=cli_extra.get("model", "qwen-turbo"),
                timeout_seconds=cli_extra.get("timeout_seconds", 60),
                base_url=cli_extra.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
                api_key=cli_extra.get("api_key", ""),
            )
        else:
            cli = cli_class(**cli_extra)

        # 构造 DecisionMaker config
        dm_config = None
        if decision_config:
            from .decision import DecisionConfig
            dm_config = DecisionConfig(
                api_key=decision_config.get("api_key", ""),
                model=decision_config.get("model", ""),
                base_url=decision_config.get("base_url", ""),
                temperature=decision_config.get("temperature", 0.0),
                timeout=decision_config.get("timeout", 10.0),
            )

        # 构造 Agent (Worker)
        from .agent import Agent

        return Agent(
            agent_id=agent_id,
            cli=cli,
            data_dir=data_dir,
            mode=mode,
            subscriptions=subscriptions,
            system_prompt=system_prompt,
            workspace_dir=workspace_dir,
            decision_config=dm_config,
            input_gates=input_gates,
            output_gates=output_gates,
            **kwargs,
        )

    @staticmethod
    def create_all(
        workers_config: dict[str, dict],
        data_dir: Path | str,
        **defaults,
    ) -> dict[str, any]:
        """批量创建 Workers.

        Args:
            workers_config: {
                "seller-fish": {"cli": "opencode", "subscriptions": ["fish-market"], ...},
                "buyer-fish": {"cli": "opencode", "subscriptions": ["fish-market"], ...},
              }
            data_dir: 数据目录
            **defaults: 默认配置, 会被每个 worker 配置覆盖

        Returns:
            {agent_id: Agent 实例}
        """
        workers = {}
        for agent_id, config in workers_config.items():
            merged = {**defaults, **config}
            workers[agent_id] = WorkerFactory.create(agent_id=agent_id, **merged, data_dir=data_dir)
        return workers


# =============================================================================
# Public exports
# =============================================================================

__all__ = [
    "WorkerFactory",
    "register_cli",
    "list_clis",
    "get_cli_class",
]
