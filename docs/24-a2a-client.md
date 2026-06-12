# 24. A2A Client 集成 (Worker 调外部 A2A Agent)

> v2.0.3 改进 — Worker 可以**通过 A2A 协议**调外部 LLM agent
> (LangChain, CrewAI, AutoGen, 自建 A2A server 等), 扩展能力.
> 不暴露我们为 A2A server (跟"会议室"核心范式不冲突).

## 概述

**A2AClient** 是一个新的 CLI adapter, 跟 `OpenCodeCLI` / `QwenCLI` / `MockCLI` 同一层.
Worker 通过 `config.json` 指定 `cli: "a2a"`, 启动时 `WorkerFactory` 创建 `A2AClient` 实例,
调用外部 A2A server (HTTP+JSON-RPC), 拿 reply 写到 channel.

**跟 EventBus/busd/watchdog 配合**:
- A2A HTTP 调用延迟: ~100-500ms (网络)
- 不影响内部感知延迟 (L1/L2/L3 仍 < 50ms)
- 一个 worker 可以同时调多个外部 A2A agent

**为什么只做 client, 不做 server**:
- A2A "client → remote" 模式跟我们的"god 当导演" 范式不冲突
- A2A "暴露 agent card 让外部调" 跟"会议室"不匹配 (外部 A2A client 找单个 agent, 但我们 1 个 channel 有多个 agent 协作)
- 做 client 让 worker **借用外部 LLM agent 能力**, 互补不冲突
- 详见 [docs/23-a2a-research.md](23-a2a-research.md) 调研

---

## 快速开始

### 1. 安装依赖

`httpx>=0.27` 已在 `pyproject.toml` 依赖中 (用于 FastAPI 测试), 无需额外安装.

### 2. Worker config.json

把 worker 的 `cli` 字段设成 `"a2a"`, 加 `a2a_url` 和可选 `a2a_api_key`:

```json
{
  "seller-fish": {
    "cli": "a2a",
    "a2a_url": "https://external-bargain-agent.example.com",
    "a2a_api_key": "secret-key-from-env-or-config",
    "mode": "proactive",
    "subscriptions": ["fish-market"],
    "default_channel": "fish-market"
  },
  "buyer-fish": {
    "cli": "opencode",
    "model": "opencode/deepseek-v4-flash-free",
    "mode": "proactive",
    "subscriptions": ["fish-market"]
  }
}
```

**字段说明**:
| 字段 | 必填 | 说明 |
|------|------|------|
| `cli` | ✅ | 必须是 `"a2a"` |
| `a2a_url` | ✅ | 外部 A2A server 的 base URL (e.g. `https://example.com/a2a`) |
| `a2a_api_key` | ❌ | Bearer token, server 需要鉴权时填 (走 `Authorization: Bearer <key>` header) |
| `timeout` | ❌ | HTTP timeout (秒, 默认 30) |

### 3. 启动

跟其他 worker 一样:

```bash
.venv/bin/python -m agents_chat.main run-worker seller-fish --data-dir ./data_v2
```

worker 启动时:
1. `WorkerFactory.create()` 检测 `cli_type == "a2a"`
2. 构造 `A2AClient(agent_url, api_key, timeout)`
3. `A2AClient` 第一次调 execute() 时拉一次 Agent Card (缓存)
4. 之后每次 execute() 通过 HTTP 调外部 agent

### 4. 数据流

```
我们 seller-fish worker
  │
  │ DecisionMaker 选 session → 调 cli.execute()
  ▼
A2AClient.execute("开价"):
  1. 检查 card 缓存 (第一次拉 GET /.well-known/agent.json)
  2. POST /v1/message/send
     { "id": "<session>", "message": { "role": "user", "parts": [{"text": "开价"}] } }
  3. 等 reply (HTTP 同步等, timeout 30s)
  4. 解析 Task.artifacts[0].parts[0].text → "100 元"
  5. 返 CLIResponse(output_text="100 元", error="", raw=<A2A Task JSON>)
  │
  ▼
DecisionMaker 拿 reply → 写 channel (跟 OpenCodeCLI 完全一致)
```

---

## 协议细节

### A2A 规范版本

实现遵循 **A2A v0.x** 规范 (Google, Linux Foundation, Apache 2.0).
具体见 [github.com/a2aproject/A2A](https://github.com/a2aproject/A2A).

我们只实现 **client 侧的 `message/send` 同步 RPC**:
- `POST {base_url}/v1/message/send`
- 构造 A2A Task dict (`id`, `message.role`, `message.parts`)
- 解析 `Task { status, artifacts }` 响应

**没实现** (Phase 2 可选):
- `message/stream` (SSE 长任务)
- `tasks/pushNotificationConfig/set` (webhook)
- Card 主动发现 (我们直接读 config)
- Server side (跟"会议室"不兼容, 留作未来)

### 鉴权

- 走标准 `Authorization: Bearer <api_key>` header
- server 端验证 (我们 client 不验)
- 没设 `a2a_api_key` → 不发 Authorization header (server 端可配置"开放访问")

### Session

- 每个 A2AClient 实例有固定 `session_id` (启动时 `new_session_id("a2a")` 生成)
- worker 调 `execute(session_id=...)` 可透传 (stateful server 端用)
- 一次 worker 启动 = 一个 session, 重启换新 session

### 错误处理

| 情况 | 行为 |
|------|------|
| HTTP timeout (默认 30s) | `CLIResponse.error = "A2AClient timeout: ..."` |
| HTTP 5xx/4xx | `CLIResponse.error = "A2AClient HTTP 500: ..."` |
| Connection refused / DNS | `CLIResponse.error = "A2AClient connection error: ..."` |
| Server 返空 artifacts | `CLIResponse.output_text = "[A2AClient no-text-reply] status=..."` |
| Server 返非 "completed" 状态 | `CLIResponse.error = "A2A status=..."` (output_text 还是返) |

**不抛异常** — 跟 OpenCodeCLI 一致, 错误通过 `CLIResponse.error` 字段表达.
DecisionMaker 看到 `error != ""` 会决定下一步 (重试 / 跳过 / 反馈 god).

---

## 真实场景示例

### 场景 1: Worker 调 LangChain RAG Agent

```json
// config.json
{
  "knowledge-bot": {
    "cli": "a2a",
    "a2a_url": "https://langchain-rag.example.com/a2a",
    "a2a_api_key_env": "LANGCHAIN_KEY",
    "mode": "proactive",
    "subscriptions": ["dev-team"]
  }
}
```

worker 在 dev-team 频道被 @, 调用 LangChain RAG 查文档:

```text
[10:30:00]  god: @knowledge-bot 查一下 FastAPI v0.110 的 middleware 文档
[10:30:00]  knowledge-bot: 调用 langchain-rag ...
[10:30:01]  knowledge-bot: → A2A POST /v1/message/send {"text": "FastAPI v0.110 middleware 文档"}
[10:30:02]  knowledge-bot: ← A2A Task { "artifacts": [{"text": "FastAPI v0.110 中间件..."}] }
[10:30:02]  knowledge-bot: @god FastAPI v0.110 中间件... (RAG 找到的)
```

### 场景 2: Worker 调 CrewAI 团队 Agent

```json
{
  "research-team": {
    "cli": "a2a",
    "a2a_url": "https://crewai-research.example.com/a2a",
    "a2a_api_key": "xxx",
    "mode": "passive"
  }
}
```

`crewai-research` 内部有 researcher + writer + reviewer, 我们 worker 调一次拿最终报告.

### 场景 3: 同时调多个 A2A Agent

```json
{
  "orchestrator": {
    "cli": "a2a",
    "a2a_url": "https://primary-agent.example.com/a2a",
    "mode": "proactive",
    "subscriptions": ["main"]
  }
}
```

orchestrator 调 primary-agent, 内部逻辑可能**再调** secondary-agent (Cascade).
A2A 协议支持这种调用链 (recursive task delegation).

---

## 配置技巧

### 1. 多个 worker 用同一个 A2A server (skill 不同)

```json
{
  "seller-fish": {
    "cli": "a2a",
    "a2a_url": "https://multi-skill-agent.example.com/a2a",
    "mode": "proactive",
    "subscriptions": ["fish-market"]
  },
  "search-bot": {
    "cli": "a2a",
    "a2a_url": "https://multi-skill-agent.example.com/a2a",  // 同 server 不同 skill
    "mode": "proactive",
    "subscriptions": ["research"]
  }
}
```

server 端通过 `metadata.agent_id` 字段区分 (我们 client 写到 A2A Task metadata).

### 2. 跟 OpenCode 混合 (本地 + 远程)

```json
{
  "fast-fish": {  // 快速响应, 本地 mock
    "cli": "mock",
    "mode": "proactive",
    "subscriptions": ["fish-market"]
  },
  "smart-fish": {  // 复杂逻辑, 远程 A2A
    "cli": "a2a",
    "a2a_url": "https://advanced-agent.example.com",
    "mode": "proactive",
    "subscriptions": ["fish-market"]
  }
}
```

两个 worker 在同 channel, god 决定让谁回复 (通过 @mention).

### 3. 鉴权 + 轮换

```json
{
  "seller-fish": {
    "cli": "a2a",
    "a2a_url": "https://rotating-keys.example.com/a2a",
    "a2a_api_key_env": "EXTERNAL_AGENT_KEY_DAILY"  // ← 改 env var 即可轮换
  }
}
```

(注: 当前实现是直接读 `a2a_api_key` 字段, 未来加 `_env` 后缀支持 env var.)

---

## 实现细节 (给开发者)

### 文件结构

```
src/agents_chat/infra/cli/a2a.py      # A2AClient (~200 行)
src/agents_chat/infra/worker_factory.py  # 注册 "a2a" + 加载 config (+30 行)
tests/unit/runtime/test_a2a_client.py  # 23 tests
docs/24-a2a-client.md                 # 本文档
docs/23-a2a-research.md               # 调研文档
```

### 关键代码

```python
# src/agents_chat/infra/cli/a2a.py
class A2AClient(CLI):
    name = "a2a"
    
    def __init__(self, agent_url, api_key=None, timeout=30.0, workspace_dir=None):
        self.agent_url = agent_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._card = None
        self.session_id = new_session_id("a2a")
    
    async def execute(self, prompt, session_id=None, workspace_dir=None) -> CLIResponse:
        await self._ensure_card()  # 拉 Agent Card (缓存)
        task = {
            "id": session_id or self.session_id,
            "message": {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            "metadata": {"agent_id": ...},
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(f"{self.agent_url}/v1/message/send", json=task, headers=headers)
                r.raise_for_status()
                result = r.json()
        except httpx.TimeoutException:
            return CLIResponse(error=f"A2AClient timeout: {self.agent_url}", ...)
        except httpx.HTTPStatusError as e:
            return CLIResponse(error=f"A2AClient HTTP {e.response.status_code}: ...", ...)
        except httpx.RequestError as e:
            return CLIResponse(error=f"A2AClient connection error: {e}", ...)
        # 解析 A2A Task
        output_text = self._extract_text(result)
        error = "" if result.get("status") == "completed" else f"A2A status={result.get('status')}"
        return CLIResponse(
            output_text=output_text,
            new_session_id=session_id or self.session_id,
            error=error,
            raw=json.dumps(result),
        )
```

```python
# src/agents_chat/infra/worker_factory.py (新增分支)
register_cli("a2a", A2AClient)

# create() 里的分发:
elif cli_type == "a2a":
    a2a_url = cli_extra.get("a2a_url")
    if not a2a_url:
        raise ValueError(f"A2AClient 需要 cli_config.a2a_url")
    cli = cli_class(
        agent_url=a2a_url,
        api_key=cli_extra.get("a2a_api_key", ""),
        timeout=cli_extra.get("timeout", 30.0),
        workspace_dir=str(workspace_dir),
    )
```

### 公共 API (re-export)

```python
from agents_chat.infra.cli.a2a import A2AClient
# 完整 CLI 接口跟 OpenCodeCLI 一致:
#   name, async execute(prompt, session_id, workspace_dir) -> CLIResponse
```

---

## 测试 (23 tests)

```bash
.venv/bin/python -m pytest tests/unit/runtime/test_a2a_client.py -v
```

| 类别 | 数量 | 覆盖 |
|------|------|------|
| `TestA2AClientBasics` | 3 | 构造, 字段, repr |
| `TestAgentCard` | 3 | 拉取, 缓存, 失败 silent |
| `TestA2AClientExecute` | 5 | 基础执行, 协议 JSON, session 透传, 鉴权 header (有/无) |
| `TestA2AClientErrors` | 4 | timeout, HTTP 4xx/5xx, 连接错, 空 reply |
| `TestExtractText` | 4 | 单/多 artifact, 非 text 跳过, 空 artifacts |
| `TestWorkerFactoryIntegration` | 4 | a2a 已注册, create worker, 缺 url 报错, API key 透传 |

完整套件: **370 passed** (347 + 23 新).

---

## 路线图 (未来)

| Phase | 内容 | 工作量 |
|-------|------|--------|
| ✅ v2.0.3 | A2AClient client-only (本文档) | 1 天 |
| Phase 2 | `a2a_url_env` 支持 env var 引用 | 1 小时 |
| Phase 2 | 流式支持 (`message/stream` SSE) | 1 天 |
| Phase 2 | Card 缓存到磁盘 (worker 重启不重新拉) | 2 小时 |
| Phase 3 | OAuth 2.0 鉴权 | 1-2 天 (跟企业 SSO 集成) |
| Phase 3 | Part (file/data) 支持 (multipart upload) | 2-3 天 |
| Phase 3 | 我们做 A2A server (Card 暴露) | 1-2 天 (需仔细设计"会议室"映射) |
| Future | Agent Federation (多 agents-chat-channel 互调) | 待设计 |

---

## 故障排查

### Q: 启动报错 "A2AClient 需要 cli_config.a2a_url"

**原因**: config.json 里 worker 的 `cli` 是 `"a2a"` 但没给 `a2a_url`.

**修**:
```json
{
  "worker-id": {
    "cli": "a2a",
    "a2a_url": "https://example.com/a2a"  // ← 加这个
  }
}
```

### Q: execute 一直 timeout

**原因**: server 不响应 / 网络问题 / timeout 太短.

**排查**:
1. `curl https://server/.well-known/agent.json` 测连通
2. 调小 timeout 测试: `"timeout": 5.0`
3. 看 server 端日志, 是不是 server 没启 / 鉴权错

### Q: reply 是空 / `[A2AClient no-text-reply]`

**原因**: A2A server 返 Task 但 artifacts 是空.

**排查**:
1. 直接 `curl POST {server}/v1/message/send` 看完整 Task 响应
2. server 可能在 `status: "input-required"` (问问题), 或 "working" (没完)
3. 我们 client 默认等 "completed", 其他状态返到 error 字段

### Q: 鉴权 401/403

**原因**: server 端要 API Key, 我们没发 / 发错了.

**排查**:
1. server `.well-known/agent.json` 看 `authentication.schemes` 是不是 `["apiKey"]`
2. config 加 `a2a_api_key` 字段
3. 测 `curl -H "Authorization: Bearer $KEY" ...`

### Q: 想用 OAuth 2.0 (不是 API Key)

**当前**: 不支持. 等 Phase 3.

**临时方案**: server 端支持 OAuth → token, 我们 client 写死 `a2a_api_key: "<oauth-access-token>"` 也能 work (但需要定期 refresh, 写个 cron 任务刷新 config).

---

## 跟 A2A 生态的关系

| 生态方 | 我们能调吗? | 怎么调? |
|--------|-----------|---------|
| **LangChain** (Python agent framework) | ✅ | LangChain 暴露 A2A endpoint (e.g. via a2a-python SDK), 我们 client 连 |
| **CrewAI** | ✅ | 同上 |
| **AutoGen** (Microsoft) | ✅ | 同上 |
| **Google ADK** | ✅ | Google ADK 支持 A2A server side |
| **自建 A2A server** (任何 A2A SDK) | ✅ | 直接给 URL + API key |
| **Cloud agents** (Bedrock AgentCore / Azure AI Foundry) | ✅ | 它们 2026 已原生 A2A |

我们不进 A2A 生态的"agent card marketplace" (我们不是被调用方), 但**所有 A2A-compatible 的 agent 都可以被我们的 worker 调**, 复用 150+ 组织的工作.

---

## 参考资源

- **A2A Spec**: https://github.com/a2aproject/A2A
- **A2A Python SDK**: https://github.com/a2aproject/a2a-python
- **调研文档**: [docs/23-a2a-research.md](23-a2a-research.md) (770 行)
- **官方 A2A 文档**: https://a2a-protocol.org/
- **Google 博客**: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability

---

## TL;DR

✅ **我们 worker 可以调外部 A2A agent** (LangChain, CrewAI, AutoGen, 自建 server).
✅ 跟 OpenCodeCLI/QwenCLI 完全同层, 不破坏架构.
✅ 1 天工作量, 23 个新测试, 全部通过.
❌ 我们不做 A2A server (跟"会议室"范式不兼容).
📌 Phase 1 完结 (client-only). Phase 2 视用户需求扩展.
