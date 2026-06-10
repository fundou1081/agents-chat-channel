# agents-chat-channel

> **Multi-agent runtime with file-bus + PDR architecture** —
> 多个 LLM 驱动的 agent 在共享频道里自主谈判, god 导演, 全部用文件总线通信.
>
> 4 组件 PDR 架构 (Perceive-Decide-Remember-Act) · 文件总线 · FastAPI + WebUI 控制台 · 23 个 CLI 适配 (opencode/qwen/mock)

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-347%20passed-brightgreen.svg)](tests/unit/runtime/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](#license)
[![Architecture: PDR](https://img.shields.io/badge/architecture-PDR-orange.svg)](docs/15-v2-architecture-overview.md)

---

## 🎬 看个 Demo — 卖鱼讨价还价

启动 2 个 agent (seller-fish + buyer-fish), god 当导演, 它们在 `fish-market` 频道自己谈价格:

```text
[19:46:18]  god:        @seller-fish @buyer-fish 你是卖鱼小贩, 开价 100 元一斤.
                          @buyer-fish 你是买鱼顾客, 预算 90 元. 开始报价吧!
[19:46:30]  seller-fish: @buyer-fish 新鲜上岸的野生大黄鱼，100块一斤，这品质菜市场至少卖150！
                          今早刚打的，你看这鱼鳃还红着呢。
[19:46:35]  buyer-fish:  @seller-fish 70一斤，市场价差不多就这样。我诚心买，可以的话直接成交。
[19:46:53]  seller-fish: @buyer-fish 95一斤，这可是今天早上刚到的鲜货！70我真进都进不来，
                          各让一步，95拿走，我再搭你两条小黄鱼。
[19:47:05]  buyer-fish:  @seller-fish 85一斤，你搭那两条小黄鱼我就认了。90我真不能再多了。
[19:47:25]  seller-fish: @buyer-fish 90一斤，两条小黄鱼搭给你。行的话直接成交，收摊了。
```

每条消息都带 `<STATUS>` 块, 解析后写到 session:

```text
session: local_seller-fish_c53f5c  task: reply_fish-market
progress: 60%   next_action: 等回复
```

**完整数据流**: god 发消息 → agent 感知 → DecisionMaker 选 session → OpenCode CLI 调 LLM → 写回频道 → 投递 mention 给对方 → 循环。

---

## 🏗️ 架构

每个 agent 是独立进程, 4 个 PDR 组件协同:

```mermaid
flowchart LR
    subgraph Channel["📁 Channel (fish-market.jsonl)"]
        Msg1["god 消息"]
        Msg2["agent 消息"]
    end
    
    subgraph Agent["🤖 Agent (4 组件)"]
        Comm["CommunicationComponent<br/>(Perceive)"]
        EH["EventHandler<br/>(Decide 触发)"]
        DM["DecisionMaker<br/>(Decide 逻辑)"]
        SM["SessionManager<br/>(Remember)"]
        CLI["CLI<br/>(Act: opencode/qwen/mock)"]
    end
    
    MB["📬 Mailbox"]
    SB["📋 StateBoard"]
    WS["📁 Workspace"]
    
    Channel -->|poll| Comm
    MB -->|poll| Comm
    Comm -->|mail/event| EH
    EH -->|decide_session| DM
    DM -->|history| SM
    DM -->|generate| CLI
    CLI -->|reply| Channel
    CLI -->|@mention| MB
    SB -.->|task| EH
    WS -.->|role.md| CLI
```

**核心模块** (`src/agents_chat/`):

| 组件 | 职责 | 实现 |
|------|------|------|
| **CommunicationComponent** | Perceive (mailbox + 频道轮询) | `core/communication.py` |
| **EventHandler** | Decide 触发 (passive + proactive) | `core/event_handler.py` |
| **DecisionMaker** | Decide 逻辑 (decide_session + decide_speak) | `core/decision.py` |
| **SessionManager** | Remember (session 持久化 JSON) | `core/session_manager.py` |
| **CLI** | Act (调外部 LLM: opencode/qwen/mock) | `infra/cli/` |

**文件总线** (`data_v2/`):

```text
data_v2/
├── channels/<name>.jsonl     # 频道消息 (jsonl append-only)
├── mailboxes/<agent>.json    # agent 邮箱 (mention 投递)
├── sessions/<agent>.json     # session 持久化
├── workspaces/<agent>/       # 每个 agent 独立 workspace (role.md 等)
├── locks/                    # 文件锁 (任务认领)
└── state_board.json          # 全局任务状态
```

**两种运行模式**:

| 模式 | 触发 | 适用 |
|------|------|------|
| **Passive** (默认) | god 主动 `@mention` → agent 响应 | 人机混合 |
| **Proactive** (v2.0 新) | agent 订阅频道, 自己决定何时说话 | 全自主 agent 社交 |

---

## 🚀 快速开始

### 安装

```bash
git clone https://github.com/fundou1081/agents-chat-channel.git
cd agents-chat-channel
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 跑一个 e2e (需要 `opencode` CLI)

```bash
# 主动模式: 2 agent 自主讨价还价 (~90s)
MAX_ROUNDS=4 TIMEOUT_SECS=90 OPENCODE_WAIT=15 \
  bash examples/e2e_bargain_new.sh

# 被动模式: god 控制节奏 (发 6 封邮件)
MAX_ROUNDS=6 TIMEOUT_SECS=180 \
  bash examples/e2e_bargain_real.sh
```

### 启 server + WebUI 控制台

```bash
.venv/bin/python -m agents_chat.server --port 8765 --data-dir ./data_v2
```

打开 <http://127.0.0.1:8765/webui/>, 看到多 team 协作控制台:

- **📊 总览** — 所有 team 卡片 + 全部 worker 状态
- **💬 频道详情** — 当前 team 消息 + 成员 + 发消息表单
- **🔴 实时聊天** — 消息流 (右侧显示所有 worker 实时 PDR 状态)
- **🤖 Workers** — 全部 agent + PDR mini 状态 + 启动/停止
- **📋 任务** — StateBoard 跨频道任务
- **📥 邮箱** — worker mailbox

### 主要 API 端点

| 端点 | 作用 |
|------|------|
| `GET /api/health` | 健康检查 |
| `GET /api/agents` | 列出所有 worker |
| `GET /api/agents/{id}/pdr-status` | agent PDR 实时状态 |
| `GET /api/agents/{id}/log?tail=10` | agent 日志 |
| `POST /api/agents/{id}/start` | 启动 agent |
| `GET /api/channels` | 列出所有频道 |
| `GET /api/channels/{name}/messages` | 频道消息 |
| `POST /api/channels/{name}/messages` | 发消息 (god 主动) |
| `GET /api/channels/{name}/member-status` | 频道成员状态 |
| `GET /api/state_board` | 全局状态板 |
| `GET /api/sessions/{id}` | worker session 列表 |
| `GET /api/mailboxes/{id}` | agent 邮箱 |
| `GET /webui/` | WebUI 控制台 |

### Python API (公共导出)

```python
from agents_chat import (
    Agent,                    # 4 组件容器
    Channel, Mailbox,         # 文件总线
    EventHandler,             # Decide 触发
    DecisionMaker,            # Decide 逻辑
    SessionManager,           # Remember
    OpenCodeCLI, MockCLI,     # CLI 适配
    WorkerFactory,            # Worker 工厂
    StateBoard,               # 全局状态板
)
```

---

## 📦 项目结构

```text
agents-chat-channel/
├── src/agents_chat/
│   ├── core/                  # PDR 核心 (业务逻辑)
│   │   ├── agent.py           # Agent 容器 (4 组件组装)
│   │   ├── communication.py   # CommunicationComponent (Perceive)
│   │   ├── event_handler.py   # EventHandler (Decide 触发)
│   │   ├── decision.py        # DecisionMaker (Decide 逻辑)
│   │   ├── session_manager.py # SessionManager (Remember)
│   │   └── status.py          # status command 解析
│   ├── infra/                 # 基础设施 (I/O + 适配器)
│   │   ├── files/             # 文件总线 (channel / mailbox / lock)
│   │   ├── cli/               # CLI 适配 (opencode / qwen / mock)
│   │   ├── gates.py           # 输入/输出 Gate 链
│   │   ├── state_board.py     # 全局状态板
│   │   ├── worker_factory.py  # Worker 工厂
│   │   ├── main.py            # CLI 入口
│   │   └── server.py          # FastAPI HTTP server
│   ├── webui/                 # WebUI 静态资源
│   │   ├── index.html
│   │   ├── app.js
│   │   └── style.css
│   ├── main.py                # 顶层 CLI 入口
│   ├── server.py              # 顶层 server 入口
│   └── __init__.py            # 公共 API re-export
├── tests/unit/runtime/        # 307 单元测试
├── examples/                  # 3 个 e2e 脚本
│   ├── e2e_bargain_real.sh    # god 控制节奏 (passive)
│   ├── e2e_bargain_new.sh     # agent 自主 (proactive)
│   └── e2e_autonomous.sh      # 全自主多 agent
├── docs/                      # 20 个架构文档
├── data_v2/                   # 运行时数据 (gitignored)
└── pyproject.toml
```

---

## 🧪 测试

```bash
# 全部测试
.venv/bin/python -m pytest tests/unit/ -q
# → 347 passed, 2 warnings in 116.48s

# 只跑 runtime tests
.venv/bin/python -m pytest tests/unit/runtime/ -q

# 跑某个模块
.venv/bin/python -m pytest tests/unit/runtime/test_decision_maker.py -v
```

---

## 📚 文档

按编号顺序读最有效:

1. [docs/15-v2-architecture-overview.md](docs/15-v2-architecture-overview.md) — **架构总览** (起点)
2. [docs/13-pdr-architecture.md](docs/13-pdr-architecture.md) — 4 组件 PDR 详细
3. [docs/18-decision-maker.md](docs/18-decision-maker.md) — DecisionMaker 设计
4. [docs/19-channel-subscription.md](docs/19-channel-subscription.md) — Channel Subscription (proactive 模式)
5. [docs/17-server-api.md](docs/17-server-api.md) — Server API 完整参考
6. [docs/21-event-driven-bus.md](docs/21-event-driven-bus.md) — 事件驱动总线 (EventBus + watchdog)
7. [docs/22-uds-bus.md](docs/22-uds-bus.md) — UDS 内存 bus (busd, 跟 server 同生命周期)

---

## 🌟 关键特性

- **🧩 PDR 架构** — Perceive-Decide-Remember 4 组件清晰分离, 业务逻辑 + I/O 适配分层
- **⚡ Event-Driven Bus (3 层)** — 进程内 `asyncio.Event` (< 1μs) + 跨进程 UDS `busd` (0.01-1ms) + 跨进程 `watchdog` 兜底 (< 50ms), 替代 1s 轮询 — 详见 [docs/21-event-driven-bus.md](docs/21-event-driven-bus.md) + [docs/22-uds-bus.md](docs/22-uds-bus.md)
- **📁 文件总线** — 全部状态在文件系统, 没有数据库, 易调试 / 易回滚 / 易分布式
- **🔄 两种模式** — Passive (god 控制) + Proactive (agent 自主), 同一套 API
- **🔌 3 个 CLI 适配** — Mock (测试) + OpenCode (本地 LLM) + Qwen (云端), 切换无侵入
- **🌐 FastAPI + WebUI** — 实时监控面板, 6 个视图覆盖所有 team 协作场景
- **🛡️ Worker Gates** — 输入/输出 Gate 链 (长度限制 / 密钥过滤 / 等等)
- **📊 StateBoard** — 跨频道任务跟踪, 任务认领用文件锁
- **🧪 347 测试** — 单元测试覆盖核心 (含 40 个 event-driven 测试), e2e 跑真实 LLM

---

## 🛠️ CLI 速查

```bash
# 初始化 data_dir
python -m agents_chat.main --data-dir ./data_v2 init

# god 发消息
python -m agents_chat.main --data-dir ./data_v2 post fish-market "@sell 开个价" --from god

# 看频道最近 20 条
python -m agents_chat.main --data-dir ./data_v2 tail fish-market 20

# 启 server
python -m agents_chat.server --port 8765 --data-dir ./data_v2
```

---

## 📄 License

MIT
