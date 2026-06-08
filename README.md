# agents-chat-channel

> **v2.0** — Multi-agent runtime with file-bus + 4 组件 PDR 架构 (Perceive-Decide-Remember-Act).
> 详见 [docs/15-v2-architecture-overview.md](docs/15-v2-architecture-overview.md).

---

## v2.0 架构

每个 agent 是独立进程, 4 组件:

| 组件 | 职责 | 实现 |
|------|------|------|
| **CommunicationComponent** | 感知 (mailbox + 频道轮询) | `src/agents_chat/v2/communication.py` |
| **EventHandler** | 决策 (passive/proactive 两种模式) | `src/agents_chat/v2/event_handler.py` |
| **SessionManager** | 记忆 (session 持久化 JSON) | `src/agents_chat/v2/session_manager.py` |
| **CLI** | 执行 (调外部 LLM: opencode/qwen/mock) | `src/agents_chat/v2/cli/` |

**文件总线**: `data_v2/channels/` + `data_v2/mailboxes/` + `data_v2/sessions/` + `data_v2/locks/`

## 快速开始

```bash
# 安装
pip install -e .

# 初始化数据目录
python -m agents_chat.v2.main init --data-dir ./data_v2

# 跑 e2e 讨价还价 (被动模式, god 控制节奏)
MAX_ROUNDS=4 TIMEOUT_SECS=240 bash examples/e2e_bargain_real.sh

# 跑 e2e 全自主 (主动模式, agent 自己发起对话)
MAX_ROUNDS=4 TIMEOUT_SECS=180 bash examples/e2e_autonomous.sh

# 跑单元测试
.venv/bin/python -m pytest tests/unit/ -q
```

## 项目结构

```
src/agents_chat/v2/
├── agent.py              # Agent 容器 (4 组件组装)
├── event_handler.py      # EventHandler (passive + proactive 模式)
├── decision.py           # DecisionMaker (decide_session + decide_speak)
├── communication.py      # CommunicationComponent (感知)
├── session_manager.py   # SessionManager (记忆)
├── scanner.py           # Scanner (后台进程, 投递 mail)
├── scheduler.py         # Scheduler (后台进程, stale task)
├── gates.py            # Worker Gates (输入/输出过滤)
├── files/              # 文件总线 (Channel / Mailbox / StateBoard)
└── cli/                # CLI 适配 (opencode / qwen / mock)

tests/unit/v2/          # 370 tests (DecisionMaker + EventHandler + Scanner + Gates)
examples/
├── e2e_bargain_real.sh  # 讨价还价 e2e (passive 模式)
└── e2e_autonomous.sh    # 全自主 e2e (proactive 模式)

docs/
├── 15-v2-architecture-overview.md   # 架构总览 (起点)
├── 13-pdr-architecture.md            # 4 组件 PDR 详细
├── 18-decision-maker.md              # DecisionMaker 设计
└── 19-channel-subscription.md        # Channel Subscription (proactive 模式)
```

## 两种运行模式

| 模式 | 触发方式 | 适用 |
|------|----------|------|
| **Passive** (默认) | Scanner 检测 @mention → 投递 mail → DecisionMaker 决定 session | 人机混合 |
| **Proactive** (v2.0 新) | 订阅频道 + 轮询 → DecisionMaker.decide_speak → CLI 生成 → 写频道 | 全自主 agent 社交 |

## 测试

```bash
# 全部测试
.venv/bin/python -m pytest tests/unit/ -q
# → 370 passed

# 只跑 v2 tests
.venv/bin/python -m pytest tests/unit/v2/ -q

# DecisionMaker tests
.venv/bin/python -m pytest tests/unit/v2/test_decision_maker.py -v
```

## License

MIT