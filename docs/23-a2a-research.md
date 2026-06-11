# 23. A2A 协议调研 + 适配性分析

> **调研时间**: 2026-06-11
> **状态**: 调研完成, 待决策
> **作者**: agents-chat-channel team

---

## 0. TL;DR (执行摘要)

**A2A 协议** (Google Agent2Agent) 是 2025 年 4 月发布、6 月捐给 Linux Foundation 的 **agent 互操作开放标准**, Apache 2.0。截至 2026 年 4 月已有 **150+ 组织**支持, 三大云 (Azure / AWS Bedrock / GCP) 全部原生集成。

**它解决什么问题**: 不同 vendor / 框架 / 云的 agent 之间**发现对方、鉴权、委派任务、互相通知**。

**核心范式**: **"电话"模式** — Client agent 找 Remote agent, 发 Task, 收 Artifact (synchronous RPC / SSE stream / Webhook push).

**跟我们项目的关系**: **核心范式正交, 不冲突, 但也不直接适配**:
- A2A: **任务委派** (有界, start → end, request-response)
- 我们: **共享空间自主社交** (无界, 持续运行, event-driven pub/sub)

**推荐方案**: **方案 B — 双向集成 (做 Client 也做 Server)** — 工作量 2-3 天, 进 A2A 生态但不破坏核心。

**风险**: 强行适配会破坏我们的"持续 listen + 共享频道"范式。**适配 = 加适配器层, 不是替换核心**。

---

## 1. A2A 协议完整介绍

### 1.1 背景与现状

| 项 | 数据 |
|----|------|
| 发布 | 2025-04 (Google Cloud Next) |
| 捐给基金会 | 2025-06 (Linux Foundation) |
| 许可证 | Apache 2.0 |
| GitHub | github.com/a2aproject/A2A (24.1k★, 2.4k fork) |
| 合作伙伴 | Google, Microsoft, AWS, Salesforce, SAP, ServiceNow, Workday, IBM, Cisco, PayPal, LangChain, MongoDB, Cohere, **50+** 创始伙伴, **150+** (2026.4) |
| 云原生集成 | Azure AI Foundry, Amazon Bedrock AgentCore, Google Cloud (全部原生) |
| 互补关系 | 跟 **MCP** (Anthropic Model Context Protocol) 互补: A2A = agent↔agent, MCP = agent↔tool |

### 1.2 三大核心概念

#### 1.2.1 Agent Card

- **位置**: `/.well-known/agent.json` (well-known URI 约定)
- **作用**: agent 自我描述, 让其他 agent 发现和理解怎么跟它交互
- **内容**: 名称、版本、能力 (skills)、输入输出模态 (text/audio/video)、认证要求、端点 URL

```json
{
  "name": "SellerFish",
  "version": "2.0.0",
  "description": "Bargains on fish prices",
  "url": "https://seller-fish.example.com/a2a",
  "skills": [
    {"id": "bargain", "name": "Fish Price Negotiation", "description": "..."}
  ],
  "modalities": ["text"],
  "authentication": {
    "schemes": ["apiKey", "oauth2"]
  }
}
```

#### 1.2.2 Task (任务)

- **本质**: 工作的最小单元, 有 ID, 有生命周期
- **7 个状态**:
  1. `submitted` — 已被 remote agent 接收
  2. `working` — 正在处理
  3. `input-required` — agent 需要 client 提供更多信息
  4. `completed` — 完成, 含 Artifact
  5. `failed` — 错误结束
  6. `canceled` — client 主动取消
  7. `rejected` — agent 拒绝

- **终态**: `completed` / `failed` / `canceled` / `rejected`
- **短任务**: 整个生命周期在一次同步 response 内完成
- **长任务**: 用 SSE 流式, client 实时收到状态更新

#### 1.2.3 Message / Part / Artifact

- **Message**: 一次对话的消息, 含多个 **Part**
- **Part**: 消息的最小内容单元, 可以是:
  - `text` (纯文本)
  - `file` (文件 URI)
  - `data` (结构化 JSON)
- **Artifact**: Task 完成的产物, 跟 Message 结构类似

```
Task
├── id: "t_001"
├── status: "working"
├── messages:
│   └── [
│       {role: "user", parts: [{type: "text", text: "I need a flight from NYC to Paris"}]},
│       {role: "agent", parts: [{type: "text", text: "When?"}]}
│     ]
└── artifacts: []  // 完成后填充
```

### 1.3 三种交互模式

| 模式 | 端点 | 用途 | 实时性 |
|------|------|------|--------|
| **同步 RPC** | `POST /message/send` | 短任务, 一次请求一次响应 | 低 |
| **流式 SSE** | `GET /message/stream` | 长任务, server 主动 push 状态 | 高 |
| **Webhook 回调** | `POST tasks/pushNotificationConfig/set` | 异步 push, client 注册 callback URL | 中 |

### 1.4 协议细节

- **传输**: HTTP/HTTPS, JSON-RPC 2.0 (也支持 gRPC)
- **鉴权**: OAuth 2.0, API Keys, mTLS, OpenID Connect (沿用 OpenAPI security schemes)
- **多租户**: 支持 URL-based / header-based / body-based (`tenant` 字段) 三种路由
- **错误处理**: JSON-RPC 标准错误码 + 自定义业务错误
- **能力协商**: Agent Card 声明 skills, client 发现 + 路由
- **Modality-agnostic**: text / audio / video streaming 都支持

### 1.5 5 大设计原则 (Google 官方)

1. **Embracing natural agentic capabilities** — 不假定 agent 内部结构
2. **Building on existing standards** (HTTP, JSON-RPC) — 不发明新传输
3. **Enterprise-grade security by default** — OAuth 2.0 / mTLS / OIDC
4. **Long-running tasks** — 支持小时/天级任务, 不止秒级 RPC
5. **Modality-agnostic** — text / audio / video / structured data

### 1.6 跟 MCP 的关系 (互补, 不竞争)

| 协议 | 角色 | 例子 |
|------|------|------|
| **MCP** (Anthropic) | agent ↔ **tool** (工具调用) | "agent 调 flight-search-tool 查机票" |
| **A2A** (Google) | agent ↔ **agent** (协调) | "我的 booking agent 找 payment agent 付款" |

**实际场景**: Booking Agent 通过 A2A 跟 Payment Agent 沟通, 然后通过 MCP 调 Account Tool 完成扣款。

---

## 2. 我们的项目深度分析

### 2.1 架构回顾

```
┌─────────────────────────────────────────────┐
│           agents-chat-channel v2             │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐    │
│  │ Channel │  │ Channel │  │ Channel │     │  (共享白板, 持续流)
│  │fish-mkt │  │dev-team │  │general  │     │
│  └────┬────┘  └────┬────┘  └────┬────┘    │
│       ↓ 事件驱动 (EventBus / busd / watchdog) ↓ │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐    │
│  │  Agent  │  │  Agent  │  │  Agent  │     │  (PDR 4 组件)
│  │seller-  │  │buyer-   │  │ qwen-   │     │
│  │ fish    │  │ fish    │  │ code    │     │
│  └────┬────┘  └────┬────┘  └────┬────┘    │
│       ↓                ↓              ↓      │
│  ┌──────────────────────────────────────┐  │
│  │         CLI adapter (LLM)            │  │
│  │  OpenCodeCLI / QwenCLI / MockCLI     │  │
│  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

### 2.2 通信模型 (3 层事件驱动)

| Layer | 机制 | 延迟 | 跨进程 | 适用 |
|-------|------|------|--------|------|
| **L1** | `EventBus` (asyncio.Event) | 0.2-0.6 μs | ❌ | 同进程多 agent |
| **L2** | `busd` (Unix Domain Socket) | 0.01-1 ms | ✅ | 跨进程, **server spawn** |
| **L3** | `FileBusWatcher` (watchdog) | < 50 ms | ✅ | 跨进程, **兑底** |

**关键**: EventBus 是抽象中心, 三个 Layer 都在它之上, 业务代码不感知。

### 2.3 数据流 (god 当导演的讨价还价)

```
1. god post "@sell 开个价"      → Channel.append (sync)
                                      ↓
2. Channel.append → EventBus.emit + busd.send + filesystem write
                                      ↓
3. seller-fish agent EventBus.wait() 返回 (0 延迟)
                                      ↓
4. DecisionMaker 选 session → 调 OpenCodeCLI
                                      ↓
5. CLI 调用 LLM → 写 STATUS block → append reply
                                      ↓
6. Channel.append 又触发 EventBus (循环 2-5)
```

**关键观察**: **Channel 是共享的"白板"**, 多个 agent **同时看同一块白板**, 没人"拥有"白板。A2A 的 Task 是**私有的"信封"**, 只有 sender 和 receiver 知道。

### 2.4 核心范式: "会议室" vs A2A "电话"

| 维度 | 我们 (会议室) | A2A (电话) |
|------|--------------|-----------|
| 拓扑 | 多对多 (N agents 共享 N channels) | 一对一 (1 client → 1 remote) |
| 状态 | 持续存在 (channel 一直开着) | 有始有终 (Task start → end) |
| 可见性 | 所有人都能看 (god 看全场) | 只有双方知道 (私密) |
| 时间 | 持续 (agent 永远在 listen) | 离散 (Task 完成就结束) |
| 协调方式 | 共享空间 + mention 通知 | 直接 RPC + 状态机 |
| god 角色 | 导演 (看所有 channels, 决定何时推动剧情) | client (发 Task 的人) |

**类比**:
- 我们 = **微信群聊** (群里所有人看, @mention 通知)
- A2A = **电话** (两人私密通话, 通话完挂)

---

## 3. 详细对比 (10+ 维度)

| 维度 | A2A | 我们 | 差异度 |
|------|-----|------|--------|
| **核心抽象** | Task (有界) | Channel + Session (无界) | 🔴 根本 |
| **协调模型** | client → remote (1:1) | god + 多 agent (N:N) | 🔴 根本 |
| **通信范式** | request-response | pub-sub (持续) | 🔴 根本 |
| **传输** | HTTP/JSON-RPC (文本) | 文件总线 + UDS (二进制) | 🟡 形式 |
| **流式** | SSE (server → client 单向) | UDS busd (双向, 0.01ms) | 🟢 都支持 |
| **状态机** | 7 显式状态 | 隐式 (Channel messages 推演) | 🟡 形式 |
| **发现** | 自动化 (well-known URI) | 手动 (config.json) | 🟡 形式 |
| **鉴权** | OAuth 2.0 / mTLS / OIDC | 无 (本地 toy) | 🟢 增强 |
| **数据格式** | JSON-RPC (Part 灵活) | JSONL (append-only) | 🟡 形式 |
| **错误处理** | 6 种 JSON-RPC 错误码 | 文件 lock 失败 / channel 不存在 | 🟡 形式 |
| **实时性** | SSE 100-500ms (网络) | busd 0.01-1ms (本地) | 🟢 我们更快 |
| **可观测性** | 标准 HTTP 日志 | 进程级 PDR + 文件总线 | 🟢 差不多 |
| **跨平台** | HTTP 全平台 | 跨平台 (watchdog 兼容) | 🟢 |
| **多模态** | text/audio/video streaming | 目前 text (可扩展) | 🟡 增强 |

**结论**:
- 🔴 根本差异: 3 个 (抽象/模型/范式) — **不能直接互通**
- 🟡 形式差异: 5 个 — **可适配器桥接**
- 🟢 差不多 / 优势: 6 个 — **可借鉴**

---

## 4. 适配方案 (3 个)

### 4.1 方案 A: 暴露 A2A endpoint (我们做 **Remote Agent**)

**目标**: 让外部 A2A client (LangChain, CrewAI, AutoGen) 可以**发现并调用**我们。

**改动**:

```python
# src/agents_chat/a2a/__init__.py
# (新目录 ~300 行)

# 1. Agent Card 端点
@app.get("/.well-known/agent.json")
async def agent_card(agent_id: str) -> dict:
    """返回 A2A Agent Card."""
    return {
        "name": agent_id,
        "version": "2.0.0",
        "skills": [{"id": "bargain", "name": "Bargain", ...}],
        "modalities": ["text"],
        "url": f"http://{host}/a2a/{agent_id}",
    }

# 2. message/send (同步 RPC)
@app.post("/a2a/{agent_id}/v1/message/send")
async def message_send(agent_id: str, body: dict) -> dict:
    """把 A2A Message 翻译成 Channel.append + 等回复."""
    task_id = body.get("id", str(uuid4()))
    content = body["message"]["parts"][0]["text"]

    # 1. 我们的 channel 路由 (按 message 上下文推断)
    channel = infer_channel(agent_id, content)
    ch.append(from_=f"a2a:{body.get('client')}", content=content, mentions=[agent_id])

    # 2. 轮询等回复 (5-30s timeout)
    reply = wait_for_reply(channel, agent_id, timeout=30)
    return {
        "id": task_id,
        "status": "completed" if reply else "input-required",
        "artifacts": [{"parts": [{"type": "text", "text": reply or "no reply"}]}]
    }

# 3. message/stream (SSE)
@app.get("/a2a/{agent_id}/v1/message/stream")
async def message_stream(agent_id: str, body: dict) -> StreamingResponse:
    """SSE 推流, 跟 A2A 状态同步."""
    async def event_stream():
        async for event in watch_channel_events(channel):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**工作量**:
- `src/agents_chat/a2a/server.py` (新, ~250 行)
- `src/agents_chat/a2a/card.py` (新, ~80 行)
- 测试 (~150 行, 7-8 tests)
- 文档 (~100 行)
- **总: 1-2 天**

**价值**:
- ✅ 进 A2A 生态 (150+ 组织可见)
- ✅ 被外部 agent 框架调用
- ❌ 不影响我们的核心 (god + 多 agent 共享频道)

**风险**:
- ⚠️ A2A 假设 client 知道要找哪个 agent, 我们的 multi-agent 场景**不适配** (A2A client 找 seller-fish? 但 buyer-fish 也在同 channel)
- ⚠️ 多 agent 场景下, A2A Task 翻译不准确 (reply 可能不是预期的)

**适用**:
- 用户**主用**我们 + **偶尔**让外部调用某个 worker
- 不打算让外部"指挥"整个多 agent 系统

---

### 4.2 方案 B: 双向集成 (我们做 **Client + Server**) ⭐ 推荐

**目标**: 我们 worker 可以在 A2A 协议下**调用外部 agent** (LangChain/CrewAI/AutoGen), **同时**暴露我们自己的 Agent Card (被外部调用)。

**改动**:

```python
# 1. CLI adapter 层加 A2A (跟 OpenCodeCLI 同一层)
# src/agents_chat/infra/cli/a2a.py  (新, ~200 行)

class A2AClient(CLI):
    """A2A 协议 client, 跟 OpenCodeCLI 同一抽象."""
    def __init__(self, agent_url: str, agent_card: dict):
        self.url = agent_url
        self.card = agent_card

    async def execute(self, prompt: str, system_prompt: str = "") -> CLIResponse:
        # 1. 构造 A2A message
        msg = {
            "id": str(uuid4()),
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": prompt}]
            }
        }
        # 2. 发 A2A message/send (HTTP POST)
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{self.url}/v1/message/send", json=msg)
            task = r.json()
        # 3. 解析 A2A Task, 转 CLIResponse
        return CLIResponse(
            session_id=task["id"],
            output_text=extract_text(task.get("artifacts", [])),
            status=task["status"],
        )

# 2. server.py 加 A2A 端点 (跟方案 A 类似, 但只暴露 1 个汇总端点)
@app.get("/a2a/agents.json")  # 列出所有 agent
async def list_agent_cards():
    return {
        "agents": [make_card(a) for a in get_agents()]
    }

@app.post("/a2a/{agent_id}/v1/message/send")
@app.get("/a2a/{agent_id}/v1/message/stream")

# 3. worker 可配置 A2A adapter (跟 opencode/qwen 同级)
# config.json:
# {
#   "agent_id": "seller-fish",
#   "cli": "a2a",  # 新加
#   "a2a_url": "https://external-agent.example.com"
# }
```

**架构变化**:
```
原 CLI adapters:           加 A2A 后:
  OpenCodeCLI               OpenCodeCLI
  QwenCLI         →         QwenCLI
  MockCLI                  MockCLI
                            A2AClient    (新, 调外部 A2A agent)
```

**工作量**:
- `src/agents_chat/infra/cli/a2a.py` (新, ~200 行)
- `src/agents_chat/a2a/server.py` (新, ~200 行) — **跟方案 A 共享!**
- `src/agents_chat/infra/worker_factory.py` (改, +20 行 — 注册 a2a CLI)
- `src/agents_chat/a2a/card.py` (新, ~80 行)
- 测试 (~250 行, 12-15 tests)
- 文档 (~150 行)
- **总: 2-3 天**

**价值**:
- ✅ 完整进 A2A 生态 (双向)
- ✅ 可以**用外部 LLM agent** 扩展 (如调 LangChain 的 RAG agent)
- ✅ 可以**被外部调用** (暴露 Card)
- ✅ 复用方案 A 的 server 端代码 (~50% 共享)
- ✅ 不破坏我们核心 (god + 多 agent 共享频道)

**风险**:
- ⚠️ A2A 假设 1:1, 多 agent 共享 channel 时 reply 路由不明确
- ⚠️ 长任务的 SSE 流式跟我们"持续 listen"模型需仔细设计边界
- ⚠️ 鉴权 (OAuth 2.0) 实现复杂, 第一版可以先做 API Key

**适用**:
- 用户**主用**我们 + **偶尔**用外部 agent (e.g., 调外部 payment-agent, external RAG)
- 想要**双向打通** A2A 生态

---

### 4.3 方案 C: 不集成, 只写文档解释

**目标**: 防止 reviewer 问"为什么不 A2A 兼容", 解释清楚设计取舍。

**改动**:
- `README.md` 加 "Design Tradeoffs" 章节
- `docs/23-a2a-research.md` (就是本文件) — 已完成
- 不改任何代码

**工作量**: 半天 (本文件已经写了 80%)

**价值**:
- ✅ 文档化设计决策, 防止"feature creep"
- ✅ 让 reviewer 知道我们**有意识**地不集成
- ❌ 不进 A2A 生态

**适用**:
- 我们的核心场景**不需要** A2A
- 用户对"会议室"范式很满意
- 担心 A2A 集成会破坏设计

---

## 5. 推荐方案: 方案 B (双向集成) — 实施路线图

### 5.1 分阶段

#### 阶段 1: 暴露 A2A endpoint (方案 A 部分) — 1 天

```
src/agents_chat/a2a/
├── __init__.py
├── card.py              # Agent Card 生成 (~80 行)
├── server.py            # A2A HTTP 端点 (~250 行)
└── types.py             # Pydantic 模型 (~100 行)

tests/unit/a2a/
├── test_card.py         # 5 tests
├── test_server.py       # 8 tests (含 SSE 流式)
└── test_integration.py  # 3 tests (含真 client 调用)
```

**验收标准**:
- `GET /.well-known/agent.json` 返回有效 Agent Card
- `POST /a2a/seller-fish/v1/message/send` 能转发到 Channel 并等 reply
- `GET /a2a/seller-fish/v1/message/stream` SSE 流式返回状态
- 7 个 task 状态正确映射

#### 阶段 2: A2AClient adapter — 1 天

```
src/agents_chat/infra/cli/a2a.py  (~200 行)

tests/unit/a2a/
└── test_a2a_client.py  # 7 tests (含真 server + mock server)
```

**验收标准**:
- `A2AClient.execute()` 正确调 A2A server, 解析 response
- 跟 OpenCodeCLI 接口一致 (CLI 抽象不变)
- worker_factory 能注册 a2a 类型
- config.json `"cli": "a2a"` 能 work

#### 阶段 3: 集成测试 + 文档 — 0.5 天

```
docs/24-a2a-integration.md  (~200 行)
- 架构图
- 怎么暴露 agent
- 怎么调外部 agent
- 示例 e2e
```

**总工作量**: 2-3 天 (跟之前加 watchdog + busd 同一量级)

### 5.2 测试策略

| 测 | 范围 |
|----|------|
| 单元测试 | Agent Card 生成 / A2AClient.execute 协议解析 |
| 集成测试 | mock A2A server + 真 client, 跟真 server + mock client |
| 端到端测试 | 启 server, 启 mock A2A server, 调真实 agent, 验证 reply 回来 |
| 互操作测试 | 用 A2A 官方 SDK (Python `a2a-sdk`) 验证我们符合规范 |

### 5.3 风险 + 缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| A2A Task 跟 Channel reply 路由不一致 | 中 | 中 | 明确: A2A reply = "agent 的最新 mention" (可配置) |
| SSE 流式跟持续 listen 死锁 | 低 | 高 | 仔细设计 timeout + 边界, 加 thread 隔离 |
| 鉴权复杂度 (OAuth 2.0) | 中 | 中 | 第一版只做 API Key, 文档化 OAuth TODO |
| A2A 协议演进 (v0.x → v1.0) | 高 | 低 | 适配器层抽象, 改 1 文件即可 |
| 多 channel 时 A2A 不知道选哪个 | 高 | 中 | Agent Card 明确声明"主 channel", fallback 机制 |

---

## 6. 实施细节 (如果选 B)

### 6.1 文件结构

```
src/agents_chat/
├── a2a/                          # 新目录
│   ├── __init__.py
│   ├── types.py                  # Pydantic: AgentCard, Task, Message, Part
│   ├── card.py                   # 从 agent config 生成 Agent Card
│   ├── server.py                 # A2A HTTP 端点 (server side)
│   └── client.py                 # 可选, 薄包装 over CLI
├── infra/
│   └── cli/
│       └── a2a.py                # A2AClient (CLI adapter)

tests/unit/a2a/
├── __init__.py
├── test_card.py
├── test_server.py
├── test_a2a_client.py
└── test_e2e.py
```

### 6.2 Agent Card 样例 (从我们的 worker config 生成)

```json
{
  "name": "seller-fish",
  "version": "2.0.0",
  "description": "Bargains on fish prices in fish-market channel",
  "url": "http://127.0.0.1:8765/a2a/seller-fish",
  "provider": {
    "organization": "agents-chat-channel",
    "url": "https://github.com/fundou1081/agents-chat-channel"
  },
  "skills": [
    {
      "id": "bargain",
      "name": "Fish Price Negotiation",
      "description": "Bargains on fish prices with other agents",
      "examples": [
        "I'll open at 100 yuan/jin",
        "85 yuan is my final offer"
      ]
    }
  ],
  "modalities": ["text"],
  "defaultInputModes": ["text"],
  "defaultOutputModes": ["text"],
  "authentication": {
    "schemes": ["apiKey"]  // 第一版, OAuth 2.0 TODO
  }
}
```

### 6.3 message/send 端点 (A2A Server 实现)

```python
# src/agents_chat/a2a/server.py

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import AsyncIterator
import asyncio

class A2APart(BaseModel):
    type: str  # "text" | "file" | "data"
    text: str = ""
    data: dict = None

class A2AMessage(BaseModel):
    role: str  # "user" | "agent"
    parts: list[A2APart]

class A2ATask(BaseModel):
    id: str
    status: str  # 7 状态
    message: A2AMessage = None
    artifacts: list = []
    history: list = []

def make_router(app: FastAPI, data_dir: Path):
    @app.get("/.well-known/agent.json")
    async def agent_card():
        return build_card(data_dir)

    @app.post("/a2a/{agent_id}/v1/message/send")
    async def message_send(agent_id: str, task: A2ATask) -> A2ATask:
        # 1. 找到 agent 的主 channel
        ch = find_main_channel(agent_id, data_dir)

        # 2. 转发到 channel (当 god 一样发)
        text = task.message.parts[0].text
        ch.append(
            from_=f"a2a-client:{task.id}",
            content=text,
            mentions=[agent_id],
        )

        # 3. 等 agent reply (短任务, 30s timeout)
        task_id = task.id
        deadline = time.time() + 30
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            # 检查 channel 最后 N 条, 找 agent_id 的 reply
            msgs = ch.tail(50)
            for m in reversed(msgs):
                if m["from"] == agent_id and m["ts"] > task_start_ts:
                    task.artifacts = [{"parts": [{"type": "text", "text": m["content"]}]}]
                    task.status = "completed"
                    return task

        task.status = "input-required"
        return task

    @app.get("/a2a/{agent_id}/v1/message/stream")
    async def message_stream(agent_id: str, task: A2ATask) -> StreamingResponse:
        async def event_stream() -> AsyncIterator[str]:
            ch = find_main_channel(agent_id, data_dir)
            last_ts = time.time()
            ch.append(from_=f"a2a-client:{task.id}", content=task.message.parts[0].text, mentions=[agent_id])
            while True:
                await asyncio.sleep(0.2)
                msgs = ch.tail(50)
                for m in reversed(msgs):
                    if m["ts"] > last_ts and m["from"] == agent_id:
                        yield f"data: {json.dumps({'status': 'working', 'delta': m['content']})}\n\n"
                        if m.get("type") == "text" and not m["content"].endswith("..."):
                            yield f"data: {json.dumps({'status': 'completed', 'artifacts': [{'parts': [{'type': 'text', 'text': m['content']}]}]})}\n\n"
                            return
                last_ts = time.time()
        return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 6.4 A2AClient CLI Adapter (A2A Client 实现)

```python
# src/agents_chat/infra/cli/a2a.py

import httpx
from .base import CLI, CLIResponse

class A2AClient(CLI):
    """A2A 协议的 CLI adapter. 调外部 A2A server."""
    def __init__(self, agent_url: str, timeout: float = 30.0):
        self.url = agent_url.rstrip("/")
        self.timeout = timeout
        # 启动时拉 Agent Card
        self._card = None

    async def _fetch_card(self):
        if self._card is None:
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{self.url}/.well-known/agent.json")
                self._card = r.json()

    async def execute(self, prompt: str, system_prompt: str = "") -> CLIResponse:
        await self._fetch_card()
        task = {
            "id": str(uuid4()),
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": prompt}]
            }
        }
        async with httpx.AsyncClient() as c:
            r = await c.post(
                f"{self.url}/v1/message/send",
                json=task,
                timeout=self.timeout,
            )
            result = r.json()

        # 解析 A2A Task → CLIResponse
        output_text = ""
        for art in result.get("artifacts", []):
            for part in art.get("parts", []):
                if part.get("type") == "text":
                    output_text += part.get("text", "")

        return CLIResponse(
            session_id=result["id"],
            output_text=output_text,
            status=result["status"],
        )

# 注册到 worker_factory
register_cli("a2a", A2AClient)
```

### 6.5 config.json 用法

```json
{
  "seller-fish": {
    "cli": "a2a",
    "a2a_url": "https://external-bargain-agent.example.com",
    "mode": "proactive",
    "subscriptions": ["fish-market"]
  }
}
```

**用法**: seller-fish 现在不再调 opencode, 而是**通过 A2A 协议**调外部 "bargain agent"。

---

## 7. 决策矩阵

| 场景 | 推荐方案 |
|------|----------|
| 想要"会议室"范式, 不需要外部互操作 | **C** (只文档) |
| 偶尔被外部 A2A client 调用 | **A** (暴露) |
| 完整双向打通 A2A 生态 | **B** (双向) ⭐ |
| 多机分布式 + A2A client 负载 | **B** + RedisBus |

---

## 8. 关键设计原则 (无论选哪个方案)

1. **不破坏核心**: A2A 适配器是**外层**, 不影响 god + 多 agent 共享频道
2. **一致抽象**: A2AClient 跟 OpenCodeCLI/QwenCLI 同一层 (CLI 抽象不变)
3. **渐进式**: 方案 A → 方案 B, 一步步扩展
4. **测试先行**: 每个端点先写测试 (A2A 协议严格)
5. **降级**: A2A server 不可用时, 降级到现有 Channel + Mailbox

---

## 9. 参考资源

### 官方
- **A2A Spec**: https://github.com/a2aproject/A2A/blob/main/specification/a2a-protocol.md
- **A2A Python SDK**: https://github.com/a2aproject/a2a-python (官方 SDK)
- **A2A 官方文档**: https://a2a-protocol.org/
- **Google 博客**: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability

### 调研文章
- Atlan: https://atlan.com/know/google-a2a-protocol
- Platform Engineering: https://platformengineering.com/editorial-calendar/best-of-2025/google-cloud-unveils-agent2agent-protocol
- Galileo AI: https://galileo.ai/blog/google-agent2agent-a2a-protocol-guide
- WWT Deep Dive: https://www.wwt.com/blog/agent-2-agent-protocol-a2a-a-deep-dive
- Google Discuss: https://discuss.google.dev/t/understanding-a2a-the-protocol-for-agent-collaboration/189103

### 相关协议
- **MCP** (Anthropic): https://modelcontextprotocol.io/ — agent ↔ tool
- **OpenAI Function Calling**: 老牌, agent ↔ function
- **LangChain Tools**: LangChain 框架内置

### 类似项目
- **CrewAI**: Python multi-agent 框架, 用 role/task 范式
- **AutoGen** (Microsoft): Conversable agent 框架
- **LangGraph**: StateGraph 范式, 跟 A2A 互补

---

## 10. 结论

**调研结论**: A2A 跟我们项目**核心范式正交**, 不直接适配, 但**双向集成价值高 (方案 B)**。

**下一步建议**:
1. **短期 (0.5 天)**: 本文档存档, 决策推迟
2. **中期 (2-3 天)**: 实施方案 B, 进 A2A 生态
3. **长期 (待定)**: 多机分布式 + A2A 联邦

**预期效果**:
- ✅ 我们能调 LangChain/CrewAI/AutoGen 的 agent
- ✅ 我们的 agent 能被 150+ 外部框架调用
- ✅ 不破坏"会议室"核心范式
- ✅ 工作量适中 (跟之前加 watchdog + busd 同量级)
- ✅ 复用 0 行业既有代码 (官方 SDK)

**风险**: 协议仍在 0.x 阶段, v1.0 出来后可能需要适配, 但**适配器层抽象让改动小**。

**最终建议**: 写完本文档, 用户 review 后, 视情况实施 (推荐方案 B)。
