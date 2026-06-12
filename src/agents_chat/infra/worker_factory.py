"""
WorkerFactory for v2.0 — CLI 注册表 + Worker 工厂.

统一创建 Worker (Agent 实例), 支持多种 CLI 后端:
  - opencode: OpenCodeCLI (opencode CLI + minimax/deepseek)
  - qwen: QwenCLI (Qwen HTTP API)
  - claude: ClaudeCLI (Claude Code CLI)  [待实现]
  - mock: MockCLI (测试用)

用法:
    from agents_chat.infra.worker_factory import WorkerFactory, register_cli

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

from ..infra.cli.base import CLI, CLIResponse
from ..infra.cli.mock import MockCLI
from ..infra.cli.opencode import OpenCodeCLI
from ..infra.cli.qwen import QwenCLI

logger = logging.getLogger(__name__)

# =============================================================================
# CLI Registry
# =============================================================================


# =============================================================================
# 默认 Worker Prompt 模板
# =============================================================================
# 所有 Worker 的基础 prompt 模板 (5 条通信规则).
# 通过 WorkerFactory.create() 的 system_prompt 或 role_template 覆盖.
#
# 变量:
#   - {agent_name}   Worker ID
#   - {role}          角色名称
#   - {example_agent}  示例: 已有 agent 列表的第一个 (供"@对方"示例)
#
# CLI 文件名:
#   - claude:   CLAUDE.md
#   - qwen:     AGENTS.md
#   - opencode: AGENTS.md (或 opencode.md)
#   - mock:     不需要
DEFAULT_WORKER_PROMPT_TEMPLATE = """你是{agent_name}。{role}。

你在一个多人协作频道中。频道管理员是你的上级。

**频道通信规则 (必须严格遵守):**
- 你的每一条回复, 必须在开头指定收信人, 格式: @名字
- 例如: @{example_agent} 你好
- 如果不确定对谁说, 就 @频道管理员
- 频道管理员的指令立即执行, 不要确认 "收到"
- 如果在角色扮演中, 收到对方台词就继续演, 保持角色不退出
- 每次回复最后一行: [STATUS] 简述 | 下一步: xxx
"""


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
# a2a: 调外部 A2A agent (LangChain/CrewAI/AutoGen 等)
from .cli.a2a import A2AClient
register_cli("a2a", A2AClient)
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
        # Workspace 配置
        role: str = "",
        skills: Optional[list[str]] = None,
        mcp_servers: Optional[list[str]] = None,
        role_template: str = "",
        init_workspace: bool = True,
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
            role: Worker 角色名称 (用于 workspace 目录命名和 roles.md)
            skills: 技能名称列表 (生成 skills/*.md 软链接)
            mcp_servers: MCP 服务名称列表 (生成 mcp/*.json 配置 stub)
            role_template: 角色模板字符串 (如果 system_prompt 为空, 用 role_template.format(role=role))
            init_workspace: True=自动初始化 workspace 目录结构 (默认 True)
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

        # Workspace 初始化 (隔离配置)
        if init_workspace:
            _init_workspace(
                workspace_dir=workspace_dir,
                cli_name=cli_type,
                role=role or agent_id,
                system_prompt=system_prompt,
                skills=skills,
                mcp_servers=mcp_servers,
                role_template=role_template,
            )

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
        elif cli_type == "a2a":
            # A2A client: 调外部 A2A server
            # cli_config 字段: a2a_url (必填), a2a_api_key (可选), timeout (可选)
            a2a_url = cli_extra.get("a2a_url")
            if not a2a_url:
                raise ValueError(
                    f"A2AClient 需要 cli_config.a2a_url (e.g. 'https://external-agent.com')"
                )
            cli = cli_class(
                agent_url=a2a_url,
                api_key=cli_extra.get("a2a_api_key", ""),
                timeout=cli_extra.get("timeout", 30.0),
                workspace_dir=str(workspace_dir),
            )
        else:
            cli = cli_class(**cli_extra)

        # 构造 DecisionMaker config
        dm_config = None
        if decision_config:
            from ..core.decision import DecisionConfig
            dm_config = DecisionConfig(
                api_key=decision_config.get("api_key", ""),
                model=decision_config.get("model", ""),
                base_url=decision_config.get("base_url", ""),
                temperature=decision_config.get("temperature", 0.0),
                timeout=decision_config.get("timeout", 10.0),
            )

        # 构造 Agent (Worker)
        from ..core.agent import Agent

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


# =============================================================================
# WorkspaceManager
# =============================================================================


class WorkspaceManager:
    """Worker 工作空间管理器.

    每个 Worker 有一个独立 workspace, 包含:
      - opencode.md / qwen.md / claude.md (CLI 引导文件)
      - roles.md (Worker 角色定义)
      - skills/ (技能文件, 可软链)
      - mcp/ (MCP 服务配置)
      - instructions/ (额外指令)
      - config.yaml (Worker 配置, 可被 WorkerFactory 读取)

    用法:
        wm = WorkspaceManager(Path("./data_v2/workspaces/seller-fish"))
        wm.init(
            role="卖鱼小贩",
            system_prompt="你是 seller-fish, 跟 buyer-fish 讨价还价...",
            skills=["fish-pricing", "bargaining"],
            mcp_servers=["fish-market-api"],
            cli_name="opencode",
        )
    """

    def __init__(self, workspace_dir: Path | str):
        self.workspace_dir = Path(workspace_dir)

    # --------------------------------------------------------------------------
    # 目录结构
    # --------------------------------------------------------------------------

    def _mkdirs(self):
        """创建标准 workspace 子目录."""
        subdirs = ["skills", "mcp", "instructions"]
        for sub in subdirs:
            (self.workspace_dir / sub).mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------------------
    # 初始化 (完整初始化一个 worker workspace)
    # --------------------------------------------------------------------------

    def init(
        self,
        role: str = "",
        system_prompt: str = "",
        skills: list[str] | None = None,
        mcp_servers: list[str] | None = None,
        cli_name: str = "opencode",
        role_template: str = "",
        extra_instructions: str = "",
        subscriptions: list[str] | None = None,
    ) -> Path:
        """初始化 worker workspace.

        Args:
            role: 角色名称 (用于生成 roles.md 标题)
            system_prompt: Worker 系统提示 (完整文本, 直接写入 roles.md)
            skills: 技能名称列表 (生成 skills/*.md 软链接, 指向全局技能目录)
            mcp_servers: MCP 服务名称列表 (生成 mcp/*.json 配置)
            cli_name: CLI 类型 ("opencode" / "qwen" / "claude" / "mock")
            role_template: 角色模板 (如果 system_prompt 为空, 用这个模板 + role 生成)
            extra_instructions: 额外指令文本 (写入 instructions/default.md)
            subscriptions: 订阅频道列表（如果有，Worker 会主动轮询这些频道）

        Returns:
            workspace_dir 路径
        """
        self._mkdirs()

        # 1. roles.md (Worker 角色定义)
        self._write_roles(role, system_prompt, role_template)

        # 2. CLI 引导文件 (opencode.md / qwen.md / claude.md)
        self._write_cli引导(cli_name, role, system_prompt)

        # 3. skills/ (软链接到全局技能目录)
        if skills:
            self._link_skills(skills)

        # 4. mcp/ (MCP 服务配置)
        if mcp_servers:
            self._setup_mcp(mcp_servers)

        # 5. instructions/
        if extra_instructions:
            (self.workspace_dir / "instructions" / "default.md").write_text(
                extra_instructions, encoding="utf-8"
            )

        # 6. config.yaml (Worker 配置快照)
        self._write_config(cli_name, role, skills or [], mcp_servers or [], subscriptions)

        return self.workspace_dir

    def _write_roles(self, role: str, system_prompt: str, role_template: str):
        if system_prompt:
            content = system_prompt
        elif role_template and role:
            content = role_template.format(role=role)
        elif role:
            content = f"# {role}\n\n你是 {role}."
        else:
            content = ""
        if content:
            (self.workspace_dir / "roles.md").write_text(content, encoding="utf-8")

    def _write_cli引导(self, cli_name: str, role: str, system_prompt: str):
        """写 CLI 引导文件 (opencode.md / qwen.md 等)."""
        filename = f"{cli_name}.md"
        # 如果 roles.md 已经有完整内容, opencode.md 可以引用它
        content = f"""# {cli_name}.md — {role or "Worker"} 引导

<!-- 自动生成, 编辑 roles.md 或 opencode.md 修改角色 -->

"""
        if system_prompt:
            content += f"""## 角色

{system_prompt}

"""
        content += """## 工作流

1. 读取 roles.md 了解你的角色
2. 读取 instructions/default.md (如果有)
3. 执行任务, 回复格式:
   - 简短回复内容
   - STATUS 块:
<!--STATUS
 session_id: {session_id}
 task_id: {task_id}
 progress: <0-100>
 summary: <你说的>
 next_action: <下一步>
 confidence: high
-->
"""
        (self.workspace_dir / filename).write_text(content, encoding="utf-8")

    def _link_skills(self, skills: list[str]):
        """软链接 skills/*.md 到全局 skills 目录.

        全局 skills 目录: WORKSPACE_TEMPLATES_DIR/skills/
        """
        skills_global = Path(__file__).parent.parent.parent.parent / "workspace_templates" / "skills"
        if skills_global.exists():
            for skill in skills:
                src = skills_global / f"{skill}.md"
                dst = self.workspace_dir / "skills" / f"{skill}.md"
                if src.exists() and not dst.exists():
                    try:
                        dst.symlink_to(src)
                    except OSError:
                        # Windows 不支持 symlink, 复制
                        import shutil
                        shutil.copy(src, dst)

    def _setup_mcp(self, mcp_servers: list[str]):
        """为 mcp_servers 生成配置 stub (待用户填充)."""
        for server in mcp_servers:
            cfg = self.workspace_dir / "mcp" / f"{server}.json"
            if not cfg.exists():
                cfg.write_text(
                    f'{{"mcp_server": "{server}", "config": {{"address": "", "api_key": ""}}}}',
                    encoding="utf-8",
                )

    def _write_config(self, cli_name: str, role: str, skills: list, mcp_servers: list, subscriptions: list = None):
        import json
        cfg = {
            "agent_id": self.workspace_dir.name,
            "role": role,
            "cli": cli_name,
            "skills": skills,
            "mcp_servers": mcp_servers,
            "workspace": str(self.workspace_dir),
        }
        if subscriptions:
            cfg["subscriptions"] = subscriptions
        cfg_path = self.workspace_dir / "config.json"
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)

    # --------------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------------

    def read_roles(self) -> str:
        roles_path = self.workspace_dir / "roles.md"
        if roles_path.exists():
            return roles_path.read_text(encoding="utf-8")
        return ""

    def list_skills(self) -> list[str]:
        skills_dir = self.workspace_dir / "skills"
        if not skills_dir.exists():
            return []
        return [p.stem for p in skills_dir.glob("*.md")]

    def list_mcp(self) -> list[str]:
        mcp_dir = self.workspace_dir / "mcp"
        if not mcp_dir.exists():
            return []
        return [p.stem for p in mcp_dir.glob("*.json")]

    def add_instruction(self, filename: str, content: str):
        """写一个指令文件到 instructions/."""
        path = self.workspace_dir / "instructions" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def get_cli引导(self, cli_name: str) -> str:
        """读指定 CLI 的引导文件."""
        path = self.workspace_dir / f"{cli_name}.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""


# =============================================================================
# WorkerFactory 集成 WorkspaceManager
# =============================================================================

def _init_workspace(
    workspace_dir: Path,
    cli_name: str,
    role: str,
    system_prompt: str,
    skills: list[str] | None,
    mcp_servers: list[str] | None,
    role_template: str,
    use_default_prompt: bool = True,
    subscriptions: list[str] | None = None,
) -> Path:
    """初始化 worker workspace (内部 helper).

    Args:
        use_default_prompt: True=如果 system_prompt 和 role_template 都为空, 用默认 5 条规则模板
        subscriptions: 订阅频道列表（如果有，Worker 会主动轮询这些频道）
    """
    wm = WorkspaceManager(workspace_dir)
    # 如果 workspace 已有 roles.md, 不覆盖 (保留用户编辑)
    roles_path = workspace_dir / "roles.md"
    prompt_to_use = system_prompt
    # 没传 system_prompt 但有 role_template, 用模板生成
    if not prompt_to_use and role_template:
        prompt_to_use = role_template.format(role=role or "Worker")
    # 都没传, 用默认 5 条规则模板
    if not prompt_to_use and use_default_prompt:
        prompt_to_use = DEFAULT_WORKER_PROMPT_TEMPLATE.format(
            agent_name=workspace_dir.name,
            role=role or "Worker",
            example_agent="频道成员",
        )
    # 合并现有 roles.md
    if roles_path.exists() and prompt_to_use:
        existing = roles_path.read_text(encoding="utf-8")
        if prompt_to_use not in existing:
            prompt_to_use = existing + "\n\n" + prompt_to_use
        else:
            prompt_to_use = existing
    wm.init(
        role=role,
        system_prompt=prompt_to_use,
        skills=skills,
        mcp_servers=mcp_servers,
        cli_name=cli_name,
        role_template=role_template,
        subscriptions=subscriptions,
    )
    return workspace_dir
