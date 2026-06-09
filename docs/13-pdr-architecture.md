# 13. v2.0 PDR 架构 — Perceive-Decide-Remember-Act

> Status: ✅ 已实施 (Step 1-6 完成, commit c8aa824-34b76e1)
> 替代 v2.0 老的 monolithic `Agent` 类 (单类包揽 5+ 职责)
> Author: 方浩博
> Date: 2026-06-07

## 0. 动机

v2.0 老架构 (`Agent` class, 538 行) 把 5+ 职责塞进 1 个类:
- 主动 poll 邮箱 / 频道 / state_board
- 决定: 续/新建 session, 选 CLI backend
- 调 LLM (opencode / qwen / mock)
- 解析 STATUS 块, 更新 session
- 写频道, second-route mentions
- 锁管理, workspace 管理

**问题**:
- 类太大 (538 行, 25+ 方法)
- 测试难 (mock 一堆内部状态)
- 扩展难 (新 backend / 新决策逻辑改 1 个文件)
- 调试难 (出了 bug 不知道在哪一层)

## 1. PDR 4 组件架构

按 **Perceive-Decide-Remember-Act** 拆 4 个独立组件, 每个独立文件 + 独立测试:

```
┌───────────────── Agent (1 worker) ──────────────────┐
│                                                    │
│  ┌──────────────────────────────────────────┐    │
│  │ 1. CommunicationComponent - "感知"        │    │
│  │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │    │
│  │  主动 pull: mailbox / channels / state_board│    │
│  │  被动 push: 新 mail event / task 变化     │    │
│  │  API 判断: is_relevant_mail / is_my_stale  │    │
│  │  输出: (event_type, event_data) async iter │    │
│  └────────┬─────────────────────────────────┘    │
│           │ 事件流                                │
│  ┌────────▼─────────────────────────────────┐    │
│  │ 2. EventHandler - "决策"                │    │
│  │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  │    │
│  │  听 comms 事件                              │    │
│  │  handle_mail:                               │    │
│  │    1. parse task_id / topic                 │    │
│  │    2. sessions.decide_session()             │    │
│  │    3. _build_prompt (含 session 上下文)    │    │
│  │    4. cli.execute(session_id, prompt, ws)  │    │
│  │    5. parse_status_block                    │    │
│  │    6. sessions.update(progress/next_action) │    │
│  │    7. 写频道 + _second_route                 │    │
│  │  handle_stale_task: 重新生成 STATUS 块    │    │
│  └────┬─────────────────┬────────────────┘    │
│       │                 │                         │
│  ┌────▼─────────┐ ┌─────▼──────────┐             │
│  │ 3. Session   │ │ 4. CLI Client  │             │
│  │   Manager   │ │ (opencode /    │             │
│  │   "记忆"    │ │  qwen / mock)  │             │
│  │             │ │   "执行"      │             │
│  │ ━━━━━━━━━━━ │ │ ━━━━━━━━━━━━━ │             │
│  │ - session_id │ │ - execute(    │             │
│  │ - topic     │ │   session_id, │             │
│  │ - content_  │ │   prompt, ws) │             │
│  │   summary   │ │ - new_session_│             │
│  │ - progress  │ │   id (返回)   │             │
│  │ - next_action│ └──────────────┘             │
│  │ - decide_   │                                  │
│  │   session() │                                  │
│  │  API:        │                                  │
│  │  decide /    │                                  │
│  │  update /    │                                  │
│  │  list        │                                  │
│  └─────────────┘                                  │
└────────────────────────────────────────────────────┘
```

## 2. 文件结构

```
src/agents_chat/v2/
├── session_manager.py     # 组件 3: Session + SessionManager
├── communication.py       # 组件 1: CommunicationComponent
├── event_handler.py     # 组件 2: EventHandler
├── agent.py               # Agent 容器 (组装 4 组件)
├── cli/
│   ├── base.py            # CLI Protocol (execute session_id, prompt, ws)
│   ├── mock.py
│   ├── opencode.py
│   └── qwen.py
├── files/                 # 文件 I/O 原语
├── scanner.py             # 频道扫描 + 路由
├── scheduler.py           # 全局调度 (stale 监控)
├── status.py              # STATUS 块解析
└── main.py                # CLI 入口

tests/unit/runtime/
├── test_session_manager.py
├── test_communication.py
├── test_event_handler.py
├── test_agent_container.py
└── (老的 files/scanner/etc.)
```

## 3. 各组件详细

### 3.1 SessionManager (记忆)

**文件**: `core/session_manager.py`
**职责**: 持久化每个 agent 的 session 列表, 决定续/新建

```python
@dataclass
class Session:
    session_id: str          # 内部 id (e.g. "local_seller_001")
    remote_id: str           # LLM session (e.g. "qwen_5f69eb9213")
    topic: str               # "鱼市砍价"
    content_summary: str     # 累积的内容摘要
    progress: int = 0        # 0-100
    next_action: str = ""     # "等 buyer 回复"
    status: str = "active"   # active | completed | paused
    task_id: str = ""        # 关联的 task
    channel: str = ""        # 关联的频道
    last_active: str = ""

class SessionManager:
    def create(topic, channel, task_id, content) -> Session
    def get(session_id) -> Session
    def list_active() -> list[Session]
    def list_by_task(task_id) -> list[Session]
    def update(session_id, progress, next_action, content_delta, status, remote_id, task_id) -> Session
    def decide_session(task_id, topic, channel) -> tuple[Session, bool]
        """核心: 智能决定续/新建.
        1. 精确: (channel, task_id) 已存在 → 续
        2. 模糊: 同 channel + topic 关键词 → 续
        3. 不命中 → 新建
        """
```

**测试**: 25 个独立 tests (CRUD / update / list / decide_session / concurrency)

### 3.2 CommunicationComponent (感知)

**文件**: `core/communication.py`
**职责**: 主动 pull 各种数据源 + 被动 push 事件, 简单 API 判断

```python
class CommunicationComponent:
    # 主动 pull (调 API)
    def poll_new_mails() -> list[dict]
        # 调 mailbox.read_and_clear
    def poll_my_active_tasks() -> list[dict]
        # 调 state_board.list_by_agent
    def poll_stale_tasks() -> list[dict]
        # 调 state_board.list_stale + 过滤 agent_id
    def poll_recent_channel(channel, since_offset) -> tuple[list, int]
        # 调 channel.read_since
    def poll_channel_members(channel) -> list[str]
        # 调 channel.list_members

    # 被动 push (事件唤醒)
    def on_new_mail()           # Scanner 投递后调
    def on_external_event()     # 通用 push
    def stop()                  # 终止感知循环

    # 简单 API 判断 (程序化, 不调 LLM)
    def is_relevant_mail(mail) -> bool
        # mention/task_broadcast/system → True
        # request_status + task 是我持有 → True
        # 其他 → False
    def filter_relevant(mails) -> list[dict]

    # 感知循环 (主)
    async def listen() -> AsyncIterator[(event_type, event_data)]
        # 启动时 yield 我持有的 active_task
        # 循环: yield mail / stale_task + wait push 事件
```

**测试**: 21 个独立 tests (poll 各 API / 过滤 / push / listen loop)

### 3.3 EventHandler (决策)

**文件**: `core/event_handler.py`
**职责**: 听 comms 事件, 决定怎么处理, 调度 sessions + cli

```python
class EventHandler:
    def __init__(comms, sessions, cli, agent_id, system_prompt, ...)

    async def run()
        # 主循环: async for event in comms.listen()
        #   事件分发给 handle_mail / handle_stale_task

    async def handle_mail(mail)
        # 1. parse task_id + topic
        # 2. sessions.decide_session() → (Session, is_new)
        # 3. _build_prompt (含 session content_summary / progress)
        # 4. cli.execute(session_id, prompt, ws) → Response
        # 5. parse_status_block → progress/next_action/summary
        # 6. sessions.update(progress, next_action, content_delta, status)
        # 7. 写频道 (含 STATUS 块)
        # 8. _second_route mentions

    async def handle_stale_task(task)
        # 调 LLM 重新生成 STATUS 块 (heartbeat)
        # 写 status_report 消息

    def _build_prompt(mail, session, task_id, topic, channel) -> str
        # 拼 system + session 上下文 + task + message + output 要求

    def _second_route(reply_text, ref_msg_id, task_id, channel, context_hint)
        # 提取 @mention, 投递 mention 邮件到目标 agent
```

**测试**: 15 个独立 tests (helpers / handle_mail 7 场景 / handle_stale / run loop)

### 3.4 CLI Client (执行)

**文件**: `infra/cli/{base,mock,opencode,qwen}.py`
**职责**: 调外部 LLM 工具, 返回回复

```python
class CLI(Protocol):
    name: str

    async def execute(
        session_id: str,         # 续; "" = 新建
        prompt: str,
        workspace_dir: str,
    ) -> CLIResponse:
        """返回 {output_text, new_session_id, ok, error}"""
```

**3 个实现**:
- `MockCLI`: 测试用, echo + STATUS, 0 token
- `OpenCodeCLI`: subprocess 调 `opencode run --model X --format json`, cwd=workspace
- `QwenCLI`: HTTP API (OpenAI-compatible), 在 prompt 里 prefix workspace.md

### 3.5 Agent 容器 (组装 + 委派)

**文件**: `core/agent.py`
**职责**: 组装 4 组件, 写 workspace 引导, 委派主循环

```python
class Agent:
    def __init__(agent_id, cli, data_dir, workspace_dir, ...):
        # 文件 IO (per agent 自己的)
        self.mailbox = Mailbox(...)
        self.state_board = StateBoard(...)
        self.channels_dir / self.lock_dir / self.workspace_dir

        # 4 组件组装
        self.sessions = SessionManager(...)
        self.comms = CommunicationComponent(...)
        self.cli = cli  # 传入
        self.event_handler = EventHandler(
            comms=self.comms, sessions=self.sessions, cli=self.cli,
            ...
        )

        # workspace 引导文件
        self._init_workspace_files()  # 写 {cli.name}.md

    async def run()
        # 委派
        await self.event_handler.run()

    def stop()
        # 委派
        self.comms.stop()
```

**测试**: 13 个集成 tests (init / run / workspace / 4 组件 wired / 兼容)

## 4. 信息流 (3 轮讨价还价)

```
T=0  god: @sell @buy 模拟讨价还价
     ↓ Scanner 投到 seller.mailbox + buyer.mailbox

T=1  [Comms.seller 主动 poll]
      comms.poll_new_mails() → [mail from god]
      comms.is_relevant_mail(mail) → True (mention)
      yield ("mail", mail)
     ↓
     [handler.seller.handle_mail]
      sessions.decide_session("task_xxx", "鱼市砍价", "fish-market") →
        没匹配, 新建 Session(local_seller_001, topic="鱼市砍价")
      cli.execute(session_id="", prompt=...) →
        OpenCodeCLI 跑, reply="100 元", remote_id="oc_xxx"
      sessions.update(progress=10, remote_id="oc_xxx", content_delta="开价 100")
      写频道: "@buy 100 一斤"
      _second_route @buy → buyer.mailbox.append

T=2  [Comms.buyer 主动 poll] (buyer 醒)
      yield ("mail", buyer_mention)
     ↓
     [handler.buyer.handle_mail]
      sessions.decide_session → 新建 Session(local_buyer_001)
      cli.execute → reply="70 块!", remote_id="oc_yyy"
      sessions.update(progress=20, content_delta="还价 70")
      写频道: "@sell 70 块!"
      _second_route @sell

T=3  seller 看到 70 块, sessions.decide_session 命中"鱼市砍价"→ 续 Session(local_seller_001)
      cli.execute(session_id="oc_xxx") → 续 → reply="最低 80"
      sessions.update(progress=50)
      写频道: "@buy 最低 80"

T=4  buyer 看到 80, 续 Session → reply="成交 80"
      progress=100 → sessions.update(status="completed")
      写频道: "🎉 80 元成交"
```

## 5. 关键设计决策

### 5.1 decide_session 匹配规则
- 精确: `(channel, task_id)` 已有 → 续
- 模糊: `channel + topic` 关键词命中 → 续
- 宽松: 同 channel+topic 跨 task 共享 session (保留 LLM context)
- 严格: 跨 channel 必新建

### 5.2 content_summary 来源
- LLM reply 解析 STATUS 块, 提取 `summary` 字段
- 累积到 session.content_summary
- 调 LLM 时作为 prompt 上下文 (历史)

### 5.3 CLI 抽象统一
- 老的 `invoke()` (老 Agent 用) → 新的 `execute()` (新 EventHandler 用)
- 老的 `resume_session` 参数 → 新的 `session_id` (跟 session_mgr 字段一致)
- 3 个 CLI (mock/opencode/qwen) 都改, 8 个 tests 同步

### 5.4 mention 路由 (跟 v2 老的 3-channel 架构一致)
- 自由 mention: 任何 @ 提到的 agent 都回 (不抢锁, "讨论"模式)
- task_broadcast: 抢锁 (单 agent 抢任务)
- 4 组件架构没改这逻辑, 只搬了实现位置 (Scanner 投 → Comm.poll → EventHandler.handle)

### 5.5 跟 PDR 模式的对应
- **Perceive** = CommunicationComponent (拉 + 推 + 简单判断)
- **Decide** = EventHandler (听事件, 决定续/新建, 调度)
- **Remember** = SessionManager (持久化 session 状态)
- **Act** = CLI Client (调 LLM 执行)

## 6. 兼容性

| 老 API | 新 API | 状态 |
|--------|--------|------|
| `agent.run()` | `agent.run()` (委派给 scheduler) | ✅ 不变 |
| `agent.stop()` | `agent.stop()` (委派给 comms) | ✅ 不变 |
| `agent.channel(name)` | `agent.channel(name)` | ✅ 不变 |
| `agent.mailbox_of(agent_id)` | `agent.mailbox_of(agent_id)` | ✅ 不变 |
| `agent.snapshot()` | `agent.snapshot()` (返回 active_sessions 字段) | ✅ 兼容 |
| `agent.trigger_immediate_tick()` | `agent.trigger_immediate_tick()` (委派给 comms.on_new_mail) | ✅ 兼容 |
| `cli.invoke(prompt, ...)` | `cli.execute(prompt, ...)` | ⚠️ 改了 (统一) |
| `SessionIndex` | `SessionManager` | ⚠️ 升级 (字段丰富) |
| `Agent._process_one(mail)` | `Agent.event_handler.handle_mail(mail)` | ⚠️ 拆到 event_handler |
| `Agent._process_batch(mails)` | `Agent.event_handler` 内部循环 | ⚠️ 移除 |

老 e2e 脚本 (`e2e_bargain.sh` / `e2e_bargain_opencode.sh` / `e2e_bargain_real.sh`) 通过 CLI 跑, 不直接调 Agent 内部方法, 仍兼容。

## 7. 测试

| 组件 | tests | 内容 |
|------|-------|------|
| SessionManager | 25 | CRUD / update / list / decide_session / concurrency |
| Communication | 21 | poll 各 API / 过滤 / push / listen loop |
| EventHandler | 15 | helpers / handle_mail 7 场景 / handle_stale / run loop |
| Agent 容器 | 13 | init / run / workspace / 4 组件 wired / 兼容 |
| **总 v2** | **182** | 全部 + 老的 files/scanner/cli 108 |

跑测试: `pytest tests/unit/ -v` (8s)
跑 e2e (mock): `bash examples/e2e_v2_4comp.sh` (15s)
跑 e2e (真 opencode): 改 `--cli opencode` (5min, 3 轮)

## 8. 关键文件统计

| 文件 | 行数 | 角色 |
|------|------|------|
| `core/session_manager.py` | 220 | Session + SessionManager |
| `core/communication.py` | 200 | CommunicationComponent |
| `core/event_handler.py` | 370 | EventHandler |
| `core/agent.py` | 250 | Agent 容器 (4 组件组装 + 兼容) |
| `infra/cli/{base,mock,opencode,qwen}.py` | ~100 each | CLI 抽象 |
| **总 v2 源** | ~1500 | 4 组件 + CLI + 工具 |

vs 老 Agent: 538 行单体 → 4 组件 × ~250 行 = 平均 250 行, 但**独立可测 + 关注分离**。

## 9. 实施时间

| 步骤 | 内容 | 估时 | 实际 |
|------|------|------|------|
| 1 | SessionManager + tests | 1h | ~30min |
| 2 | CommunicationComponent + tests | 1.5h | ~30min |
| 3 | EventHandler + tests | 1.5h | ~1h (含 CLI 名字统一) |
| 4 | Agent 容器 + tests | 30min | ~30min |
| 5 | e2e (mock) | 1.5h | ~1h |
| 6 | docs | 30min | - |
| **总** | | **6.5h** | **~3.5h** |

## 10. 下一步

- [ ] **依赖解析**: 解析 STATUS.next_action 自动唤醒相关 agent
- [ ] **负载均衡**: 堆积多任务时降优先级
- [ ] **Web 面板**: state_board 实时可视化
- [ ] **更多 CLI**: 加 `claude` CLI (Anthropic Claude Code)
- [ ] **混合 CLI**: 一个 agent 偶尔用 opencode + 偶尔用 qwen
- [ ] **真讨价还价 3 轮**: 改进 opencode.md prompt + 测试, 真的让两个 agent 轮 3 轮 (目前是并行各 1 轮)
