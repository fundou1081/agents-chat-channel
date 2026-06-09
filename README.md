# agents-chat-channel

> **v2.0** — Multi-agent runtime with file-bus + 4 组件 PDR 架构 (Perceive-Decide-Remember-Act).
> 详见 [docs/15-v2-architecture-overview.md](docs/15-v2-architecture-overview.md).

---

## v2.0 架构

每个 agent 是独立进程, 4 组件 (PDR 核心):

| 组件 | 职责 | 实现 |
|------|------|------|
| **CommunicationComponent** | Perceive (mailbox + 频道轮询) | `src/agents_chat/core/communication.py` |
| **EventHandler** | Decide 触发 (passive + proactive) | `src/agents_chat/core/event_handler.py` |
| **DecisionMaker** | Decide 逻辑 (decide_session + decide_speak) | `src/agents_chat/core/decision.py` |
| **SessionManager** | Remember (session 持久化 JSON) | `src/agents_chat/core/session_manager.py` |
| **CLI** | 执行 (调外部 LLM: opencode/qwen/mock) | `src/agents_chat/infra/cli/` |

**文件总线**: `data_v2/channels/` + `data_v2/mailboxes/` + `data_v2/sessions/` + `data_v2/locks/`

## 快速开始

### 使用 Conda 环境 (推荐)

```bash
# 创建 conda 环境
conda env create -f environment.yml
conda activate agents-chat-channel

# 安装项目
pip install -e .
```

### 使用 pip

```bash
# 安装
pip install -e .

# 或者使用虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\Scripts\activate   # Windows
pip install -e .
```

# 初始化数据目录
python -m agents_chat.main init --data-dir ./data_v2

# 跑 e2e 讨价还价 (被动模式, god 控制节奏)
MAX_ROUNDS=4 TIMEOUT_SECS=240 bash examples/e2e_bargain_real.sh

# 跑 e2e 全自主 (主动模式, agent 自己发起对话)
MAX_ROUNDS=4 TIMEOUT_SECS=180 bash examples/e2e_autonomous.sh

# 跑单元测试
.venv/bin/python -m pytest tests/unit/ -q
```

## 项目结构

```
agents-chat-channel/
├── src/agents_chat/
│   ├── core/                      # PDR 核心 (业务逻辑)
│   │   ├── agent.py               # Agent 容器 (4 组件组装)
│   │   ├── communication.py       # CommunicationComponent (Perceive)
│   │   ├── event_handler.py       # EventHandler (Decide 触发, passive+proactive)
│   │   ├── decision.py            # DecisionMaker (Decide 逻辑)
│   │   ├── session_manager.py     # SessionManager (Remember)
│   │   └── status.py              # status command 解析
│   ├── infra/                     # 基础设施 (I/O + 适配器)
│   │   ├── files/                 # 文件总线 (channel / mailbox / lock)
│   │   ├── cli/                   # CLI 适配 (opencode / qwen / mock)
│   │   ├── gates.py               # Worker Gates (输入/输出过滤)
│   │   ├── state_board.py         # 全局状态板
│   │   ├── worker_factory.py      # Worker 工厂
│   │   ├── main.py                # CLI 入口 (init/run-worker/post/...)
│   │   └── server.py              # FastAPI HTTP server
│   ├── webui/                     # WebUI 静态资源 (v2.0 控制台)
│   │   ├── index.html
│   │   ├── app.js
│   │   └── style.css
│   ├── main.py                    # CLI 入口 (`python -m agents_chat.main`)
│   ├── server.py                  # FastAPI server 入口 (`python -m agents_chat.server`)
│   └── __init__.py                # 公共 API re-export (`from agents_chat import Agent, ...`)
├── tests/unit/                    # 307 单元测试
│   └── runtime/                   # 跟 src/agents_chat/{core,infra} 对应
├── examples/
│   ├── e2e_bargain_real.sh        # 讨价还价 e2e (passive 模式)
│   ├── e2e_bargain_new.sh         # 讨价还价 e2e (新)
│   └── e2e_autonomous.sh          # 全自主 e2e (proactive 模式)
├── docs/                          # 架构设计文档 (01-20)
├── archive/                       # 旧版存档 (v1 + 接手人计划)
├── data_v2/                       # 运行时数据 (gitignored)
└── pyproject.toml
```

**重要约定**:
- 所有 agent 进程都走 `agents_chat.infra.main:cmd_init` / `cmd_run_worker` 初始化
- 子进程启动走 `agents_chat.infra.server` 调 `subprocess` 跑 `python -m agents_chat.main run-worker`
- WebUI 静态文件跟 server 代码同包, 部署时只需 copy `src/agents_chat/` 即可

## 两种运行模式

| 模式 | 触发方式 | 适用 |
|------|----------|------|
| **Passive** (默认) | Scanner 检测 @mention → 投递 mail → DecisionMaker 决定 session | 人机混合 |
| **Proactive** (v2.0 新) | 订阅频道 + 轮询 → DecisionMaker.decide_speak → CLI 生成 → 写频道 | 全自主 agent 社交 |

## 测试

```bash
# 全部测试
.venv/bin/python -m pytest tests/unit/ -q
# → 307 passed

# 只跑 v2 tests
.venv/bin/python -m pytest tests/unit/v2/ -q

# DecisionMaker tests
.venv/bin/python -m pytest tests/unit/v2/test_decision_maker.py -v
```

## License

MIT