# 01 — Author Abstraction

## 核心问题: 现在的 sub-agent 是什么?

**大部分 sub-agent 框架** (AutoGen AssistantAgent, LangGraph subgraph, CrewAI Agent)
**实质上都是"披着 agent 外衣的函数"**:

```python
async def run_sub_agent(task: str) -> str:
    response = await llm.create(messages=[SystemMessage(system), HumanMessage(task)])
    return response.content
```

本质是: **一次 LLM 调用 = 整个 agent**。没有持久状态,没有 inbox,没有 heartbeat。
不算 autonomous,只算 "function with memory"。

## 重新定义: Author

> **每个 agent 是一个 Author (作者)** —— 持续活着的角色,有自己的 identity、inbox、memory、内部多个并行 session。**不是被调起来的 worker,而是自己活着、自己决策的实体。**

### Author 的 5 个核心属性

| 属性 | 含义 | 类比 |
|------|------|------|
| **Identity** | 持久身份 (Persona) | 员工名 + 角色 |
| **Inbox** | 收件箱,持久化 | 邮箱 |
| **Outbox** | 发件箱 | 发邮件 |
| **Sessions** | 内部多个并行会话 | 脑子里多个 thread |
| **Heartbeat** | 定时 tick,自主决策 | 周期性检查邮件 |

### Author 跟 Worker 的对比

| 维度 | Worker (AutoGen 风格) | Author (我们的设计) |
|------|----------------------|-------------------|
| 生命周期 | 任务来 → 启 → 完 → 死 | **一直活着** |
| 并发 | 1 个 task = 1 个对话 | **1 个 author = N 个 session 并行** |
| 通信 | push, 同步 | **pull, 异步, batch** |
| 状态 | 任务内临时 | **长期持久** |
| 决策 | 任务驱动 | **heartbeat 驱动** |
| 失败 | 整个 task 死 | **邮件还留着, 下次 tick 起来** |
| 类比 | 函数调用 | 真人员工 |

## 一次 Tick 的生命周期

```
[Author Z 醒来 (heartbeat)]
         ↓
1. 拉新邮件 (MailboxDB.fetch_unread)
         ↓
2. 更新 sessions (新邮件进对应 session)
         ↓
3. 重新加载 active sessions
         ↓
4. 构造 TickContext
         ↓
5. LLM 决策 (decide)
         ↓
6. 执行 (发邮件, 调工具, 关闭 session)
         ↓
7. 标记邮件已读
         ↓
8. 写 tick log 到磁盘
         ↓
9. 状态: idle / working / blocked / stalled
         ↓
[Author Z 继续睡 (next heartbeat)]
```

## 为什么这是对的?

### 1. 跟真实人类对齐
人就是这样的: 多个项目并行,定期检查邮件,每个邮件是一个独立 thread,基于 system prompt 决策。

### 2. 解耦 sender / receiver
发件人不知道收件人什么时候 tick。 不需要排队、不需要等响应。

### 3. 失败恢复
如果 author 崩了, 邮件在 mailbox 里。 下次启动继续。

### 4. 多 session 并行
一个 author 内部 4 个 session 同时推进。 跟 LLM 一次只能生成一个输出的限制不冲突 (因为 session 之间用状态机协调, 不是用同步调用)。

### 5. 跟 Erlang/Elixir 的 actor model 同源
Erlang 的 process 也有 mailbox, receive, 状态。 我们借鉴了核心思想。

## 跟鸡尾酒会问题的关系

**鸡尾酒会问题** (谁说话, turn-taking) 跟 **并行执行** (谁干活) 是两个独立维度。

- **turn-taking** 由 UI / 上帝 / 显式圆桌模式控制 (Round Table 视图)
- **并行执行** 是默认模式, 在 War Room / Magentic-One 模式里

**Author 抽象同时支持两种**:
- 默认: 作者之间用邮件异步通信 (不需要 turn-taking, 没人"说话")
- 显式: 切换到 Round Table 模式, LLM 选人, turn-taking

## 数据模型

```python
@dataclass(frozen=True)
class Mail:
    """一封邮件 = 一条异步消息"""
    id: str
    sender: str
    recipients: tuple[str, ...]
    thread_id: str
    in_reply_to: str | None
    subject: str
    body: str
    priority: int
    requires_ack: bool
    created_at: datetime

@dataclass
class SessionContext:
    """一个 author 内部的一个会话 (类似人脑子里一个 thread)"""
    thread_id: str
    topic: str
    status: Literal["active", "blocked", "completed", "stalled"]
    participants: set[str]
    history_ids: list[str]
    blocked_reason: str | None
    last_activity: datetime
    summary: str

@dataclass
class Persona:
    id: str
    display_name: str
    emoji: str
    title: str
    system_prompt: str
    workdir: str
    heartbeat_seconds: int
    sleep_hours: tuple[int, int] | None
    off_duty_interval: int

class Author:
    persona: Persona
    mailbox: MailboxDB
    sessions_db: SessionDB
    llm: MockLLM
    registry: HeartbeatRegistry | None

    status: AuthorStatus
    sessions: dict[str, SessionContext]

    # 生命周期
    async def start()
    async def stop()
    def trigger_immediate_tick()

    # 心跳
    async def _heartbeat_loop()
    async def _tick()
    async def _execute(decision, new_mail)

    # 持久化
    async def _load_sessions()
    async def _write_tick_log()

    # 观察
    def snapshot() -> dict
```

## 实现

- `src/agents_chat/models.py` — Mail, Session, Persona, Decision
- `src/agents_chat/storage/mailbox_db.py` — SQLite 邮箱
- `src/agents_chat/storage/session_db.py` — SQLite session
- `src/agents_chat/author/base.py` — Author 类
- `src/agents_chat/author/think.py` — prompt builder + decide
- `src/agents_chat/llm/mock.py` — Mock LLM
- `src/agents_chat/heartbeat.py` — Registry

## 验证

19 个单元 + 集成测试通过。 跑 `python -m agents_chat.main demo` 看 3 个 author 自主并行运转。
