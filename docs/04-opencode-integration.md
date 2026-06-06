# 04 — OpenCode Integration

## 目标

把本地 OpenCode CLI 当 author 的 "think backend", 让 author 能真正改文件、跑命令、干活。

## 架构

```
┌─────────────────┐
│  Author         │  (mailbox + heartbeat + sessions, 我们自己管)
│  ┌───────────┐  │
│  │ Think     │──┼──→ OpenCodeAgent (新)
│  └───────────┘  │         ↓
└─────────────────┘    opencode run --format json
                            ↓
                      真干活: 改文件 / 跑 bash / 读 read
                            ↓
                      NDJSON events → 解析 → Decision
```

## 关键设计决策

### 1. OpenCode 是 LLM 替代品,不是工具

我们让 `OpenCodeAgent` 实现跟 `MockLLM` 一样的接口:

```python
async def think(system, user, ctx=None, tools=None) -> Decision
```

- MockLLM: 规则-based, 快速
- OpenCodeAgent: 调 CLI, 真的干活
- 未来: ClaudeCodeAgent, CodexAgent 都用同样接口

### 2. 每次 tick 起新 process

不维护 long-lived opencode process (虽然 `--attach` 模式可以), 简单优先。

- 优点: 简单, 无状态
- 缺点: 启动慢 (5-10s/次), 每次完整 token cost

### 3. NDJSON 流式读取

opencode 输出 NDJSON 事件流 (`step_start`, `tool_use`, `text`, `step_finish`)。 我们用 `readline()` 流式读取每行, 实时 parse, 不依赖 process 退出时的 stdout flush。

```python
async def read_stdout():
    while True:
        line = await proc.stdout.readline()
        if not line:
            return
        events.append(json.loads(line))
```

### 4. 强制 JSON 输出

opencode 默认是 narrative text。 我们在 prompt 里强制 "**最后必须输出 JSON 决策**"。 单独跑 minimax-m3-free 模型, 它能遵守。

### 5. Sandbox permission

opencode 默认拒绝写到 `--dir` 之外的路径。 我们用 `--dangerously-skip-permissions` 跳过 (因为我们的 author workdir 是 `/tmp/agents-chat-workdirs/<name>/`, opencode 可能想探索父目录)。

### 6. Workdir 隔离

每个 author 有独立 workdir:

```python
BUILTIN_PERSONAS = {
    "zhang": Persona(workdir="/tmp/agents-chat-workdirs/zhang", ...),
    "li": Persona(workdir="/tmp/agents-chat-workdirs/li", ...),
    "pm": Persona(workdir="/tmp/agents-chat-workdirs/pm", ...),
}
```

opencode 跑在 workdir 里, 改文件不互相影响。

## 验证

### 单元测试 + 集成测试 (17/17)

- 15 unit (mailbox / session / think / format)
- 2 integration (opencode echo + opencode create file)

### Manual demo

```
$ python -m agents_chat.main web --llm opencode

# 浏览器开 http://localhost:7333
# 发邮件给 zhang-frontend: "写 calculator.py, 含 add/sub/mul, 跑 python3 验证"

# 30-60s 后:
# - /tmp/agents-chat-workdirs/zhang/calculator.py 被创建
# - zhang 回信给 god: "已完成 ✅"
```

实测 1 tick 完成 1 文件创建 + 1 邮件发送, 全程 opencode 自主决策。

## 已知问题 / 后续优化

| 问题 | 现状 | 解决方向 |
|---|---|---|
| 启动慢 (5-10s/次) | 每次冷启 opencode | 跑 `opencode serve` + `--attach` 复用 |
| 派活链 (PM → zhang) 不可靠 | PM 经常自己干, 不派活 | 调 prompt 强约束 / 换更小的模型 |
| Token 浪费 (每次新 session) | 重复 system prompt | `--session` 复用 |
| 工具调用慢 (一 tick 30-50s) | opencode agent loop 多步 | 限制工具调用轮数 (Phase 2) |

## 用法

```bash
# 默认 mock (快, 测试用)
python -m agents_chat.main demo

# 真 opencode (慢, 真干活)
python -m agents_chat.main demo --llm opencode

# 自定义模型
python -m agents_chat.main demo --llm opencode --model opencode/minimax-m3-free
```

## 文件位置

- `src/agents_chat/llm/opencode.py` — OpenCodeAgent 类
- `src/agents_chat/main.py` — `--llm` 切换
- `tests/integration/test_opencode.py` — 集成测试
