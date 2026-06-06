# 03 — 跟 AutoGen 的对比

我们研究过 AutoGen 0.4+ (https://github.com/microsoft/autogen), 主要看 4 种 group chat 模式。 借鉴 + 改 + 避坑, 总结如下。

## AutoGen 4 种 group chat 模式

| 模式 | 调度 | 同时说话数 | 适用 |
|------|------|----------|------|
| **RoundRobinGroupChat** | 固定顺序 | 1 | 流水线任务 |
| **SelectorGroupChat** | LLM 选下一人 | 1 | 开放讨论, 话题未知 |
| **Swarm** | agent 自决 handoff | 1 | 任务边界清晰 |
| **MagenticOneGroupChat** | 显式 Orchestrator | 1 | 复杂任务 + 容错 |

**所有模式都满足**: 任意时刻只调 1 个 agent 说话。 这是 AutoGen 共识。

## 我们学什么

### ✅ 借鉴的设计

1. **Magentic-One 的 ledger 模式** (facts/plan/progress)
   - 复杂任务需要显式 state, 不只是 "传消息"
   - 我们的 Author 加 `Ledger` 类 (Phase 2+)

2. **Selector 的 `_mentioned_agents` regex + retry**
   - 选人时用 regex 解析 LLM 输出
   - 选错就 retry with feedback
   - 我们的 Round Table 模式用这个

3. **Swarm 的 `HandoffMessage`**
   - 跨 agent 转交任务的标准协议
   - 我们已经在 Mail 里支持 `thread_id` + `in_reply_to`, 自然支持 handoff

4. **eligibility_policies**
   - "谁能被选" 是个 policy, 不是 hardcode
   - 我们的 Trigger 系统借鉴

## 我们改什么

### 🔧 AutoGen 没做好的

1. **Broadcast 浪费** (issue #489)
   - AutoGen 把全量 history 推给每个 agent (push 阶段), 即使该 agent 不生成回复
   - **我们按需拼装**: Author 看到的是新邮件 + 自己的 active sessions 摘要, 不是全量 history

2. **没有本地 UI**
   - AutoGen 纯 Python, 想看状态机要 print log
   - **我们有 Web UI** (FastAPI + HTML), 实时看 inbox / sessions / tick 状态

3. **没有成本可观测**
   - AutoGen 不统计 token / 成本
   - **我们会加**: `Author.snapshot()` 返回 token 消耗, UI 显示

4. **生命周期太短**
   - AutoGen Worker 一次任务, 启 / 完 / 死
   - **Author 长生命**: 一直在跑, 管理多个 session

## 我们避什么

### ❌ AutoGen 的已知坑

1. **Selector 选错死循环** (`max_retries_for_selecting_speaker`)
   - 我们 mock LLM 里: `if thread history >= 5: close session`, 防止 Re: 死循环

2. **Magentic-One Orchestrator 单点失败**
   - 我们 Orchestrator 也可能挂, 但因为我们没有中央 Orchestrator, 这个风险降低
   - 后期: Orchestrator 也是个 Author, 挂了可以重启

3. **Swarm 讨论场景失效**
   - Swarm 在 agent 自决 "我干不了, 转给 X" 模式下, 讨论场景无人先开口
   - 我们的 Round Table 模式用 Selector (LLM 强制选人), 不依赖自决

4. **全量 history 注入**
   - AutoGen 把所有消息推给每个 agent
   - 我们只给相关 context (新邮件 + 自己 active session), 节省 token

## 并行度的差异

| | AutoGen | 我们 (Author) |
|---|---|---|
| **任务并行** | ❌ (所有 group chat 串行) | ✅ (每个 author 独立 heartbeat) |
| **多 session 并行** | ❌ (一个 worker 一个对话) | ✅ (一个 author N 个 session) |
| **跨 author 异步** | ❌ (push 同步) | ✅ (pull 异步 + burst) |
| **失败隔离** | ❌ (一个挂全挂) | ✅ (邮件不丢, 各自重启) |

## 代码量对比 (粗估)

| | AutoGen | 我们 MVP |
|---|---|---|
| 核心代码 | ~3000 行 (Rust + Python) | ~800 行 (纯 Python) |
| 依赖 | 复杂 (pydantic, autogen-core) | 轻 (FastAPI, aiosqlite) |
| 启动 | 要装 autogen-stack | `pip install -e .` |

**我们不追求 AutoGen 那么完整**, 我们追求:
- 抽象对 (Author 替代 Worker)
- 跑得动 (3 author 自主并行)
- 可观察 (Web UI 实时)
- 易扩展 (Phase 2+ 接真实 LLM)
