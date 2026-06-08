# Channel 管理: 管理员 + Worker 白名单 + WorkerFactory

> 关联: `docs/15-v2-architecture-overview.md`

## 1. Channel 管理员

每个频道可以有 **admin** (管理员). admin 的作用:

- 人类 admin 可以配置频道 (加 members / enabled_workers)
- admin 发消息时, Scanner 有特殊路由逻辑 (优先投递)

### API

```python
from agents_chat.v2.files.channel import Channel

ch = Channel("./data_v2/channels/fish-market.jsonl", "fish-market")

# 加 admin (worker agent)
ch.add_admin("god", is_worker=True)   # 有 mailbox 的 worker agent

# 加 admin (人类)
ch.add_admin("fanghao", is_worker=False)  # 人类, 无 mailbox

# 检查
ch.is_admin("god")           # True (任意类型)
ch.is_admin("god", is_worker=True)   # True (只看 worker)
ch.is_admin("fanghao", is_worker=False)  # True (只看 human)
ch.list_admins()             # worker admins
ch.list_human_admins()       # human admins
```

### Scanner 路由中的 admin 逻辑

Scanner 检测 `@admin` / `@god` / `@频道管理员` 等关键字时:
1. 如果频道有 **worker admin** (有 mailbox) → 优先投递给 worker admin
2. 如果没有 worker admin → 投递给 human admin (如果有的话)

## 2. Worker 白名单 (enabled_workers)

频道可以限制**只有某些 worker 能收到消息**. 这用于:
- 频道只允许特定角色参与
- 避免无关 agent 被误投递

### 机制

```
白名单为空 (= [])  → 不限制, 所有 members 都收得到 (向后兼容)
白名单非空         → 只有在白名单里的 worker 才收得到
```

Scanner 投递时检查: `ch.is_enabled(agent_id)` → 不在白名单就跳过.

### API

```python
# 加单个 worker 到白名单
ch.add_enabled_worker("seller-fish")

# 批量设白名单 (覆盖)
ch.set_enabled_workers(["seller-fish", "buyer-fish"])

# 清空白名单 (不限制)
ch.set_enabled_workers([])

# 检查
ch.list_enabled_workers()   # ["seller-fish", "buyer-fish"]
ch.has_restriction()        # True/False
ch.is_enabled("seller-fish")   # True
ch.is_enabled("god")        # False (不在白名单)

# 移除
ch.remove_enabled_worker("seller-fish")
```

### e2e 演示

```bash
# e2e_autonomous.sh 和 e2e_bargain_real.sh 已配置:
ch.add_admin("god")              # god 是管理员
ch.set_enabled_workers(["seller-fish", "buyer-fish"])  # 只允许这两个 worker
# → 其他即使在 members 里也收不到消息
```

## 3. WorkerFactory (Worker 工厂)

WorkerFactory 统一创建 Agent 实例, 支持多种 CLI 后端.

### 支持的 CLI

| CLI 类型 | 类 | 说明 |
|---------|---|------|
| `opencode` | `OpenCodeCLI` | opencode CLI + minimax/deepseek 模型 |
| `qwen` | `QwenCLI` | Qwen HTTP API |
| `mock` | `MockCLI` | 测试用固定响应 |
| `claude` | (待实现) | Claude Code CLI |

### 注册新 CLI

```python
from agents_chat.v2.worker_factory import register_cli
from agents_chat.v2.cli.base import CLI

class ClaudeCLI(CLI):
    name = "claude"
    async def execute(self, prompt, session_id=None, workspace_dir=None):
        ...

register_cli("claude", ClaudeCLI)
```

### 创建 Worker

```python
from agents_chat.v2.worker_factory import WorkerFactory, list_clis

# 单个创建
worker = WorkerFactory.create(
    agent_id="seller-fish",
    cli_type="opencode",
    data_dir=Path("./data_v2"),
    mode="proactive",
    subscriptions=["fish-market"],
    system_prompt="你是卖鱼小贩...",
    cli_config={"model": "opencode/deepseek-v4-flash-free"},
    decision_config={"api_key": "...", "model": "gpt-4"},
)
```

### 批量创建

```python
workers = WorkerFactory.create_all(
    {
        "seller-fish": {
            "cli_type": "opencode",
            "subscriptions": ["fish-market"],
            "system_prompt": "你是卖鱼小贩",
        },
        "buyer-fish": {
            "cli_type": "opencode",
            "subscriptions": ["fish-market"],
            "system_prompt": "你是买方",
        },
    },
    data_dir=Path("./data_v2"),
    mode="proactive",
)
```

### CLI 配置参数

| CLI | 参数 | 说明 |
|-----|------|------|
| `opencode` | `model` | 模型, 默认 `opencode/deepseek-v4-flash-free` |
| `opencode` | `timeout_seconds` | 超时, 默认 300 |
| `opencode` | `binary` | 二进制路径, 默认 `opencode` |
| `qwen` | `model` | 模型, 默认 `qwen-turbo` |
| `qwen` | `api_key` | API Key |
| `qwen` | `base_url` | API 地址 |
| `mock` | (无) | 测试用 |

## 4. 快速参考

```python
# 1. Channel: 加管理员 + 白名单
ch = Channel("./data_v2/channels/fish-market.jsonl", "fish-market")
ch.add_admin("god")
ch.set_enabled_workers(["seller-fish", "buyer-fish"])

# 2. WorkerFactory: 创建 workers
from agents_chat.v2.worker_factory import WorkerFactory
workers = WorkerFactory.create_all({
    "seller-fish": {"cli_type": "opencode", "subscriptions": ["fish-market"]},
    "buyer-fish": {"cli_type": "opencode", "subscriptions": ["fish-market"]},
}, data_dir=Path("./data_v2"), mode="proactive")

# 3. 启动
for w in workers.values():
    asyncio.create_task(w.run())
```

## 5. Workspace 目录结构

每个 Worker 独立 workspace, 由 `WorkspaceManager` 自动创建:

```
workspaces/{agent_id}/
├── roles.md           # Worker 角色定义 (strategy / 行为规则)
├── {cli}.md           # CLI 引导文件 (opencode.md / qwen.md / claude.md)
├── skills/            # 技能软链接 (指向全局 workspace_templates/skills/)
│   ├── bargaining.md  → workspace_templates/skills/bargaining.md
│   └── fish-pricing.md
├── mcp/               # MCP 服务配置 stub
│   └── fish-market-api.json
├── instructions/      # 额外指令
│   └── default.md
└── config.json        # Worker 配置快照 (cli / role / skills / mcp_servers)
```

### 自动初始化

`WorkerFactory.create(init_workspace=True)` 时自动调用:

```python
from agents_chat.v2.worker_factory import WorkerFactory

worker = WorkerFactory.create(
    agent_id="seller-fish",
    cli_type="opencode",
    data_dir=Path("./data_v2"),
    role="卖鱼小贩",
    role_template=SELLER_ROLE,
    skills=["bargaining", "fish-pricing"],
    mcp_servers=["fish-market-api"],
    # → 自动创建 workspaces/seller-fish/{roles.md, opencode.md, skills/, mcp/, ...}
)
```

### 保留用户编辑

如果 `roles.md` 已存在, `_init_workspace` 会**合并** (追加 system_prompt), 不覆盖用户编辑.
