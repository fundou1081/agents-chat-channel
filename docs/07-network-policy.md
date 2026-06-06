# 07 — Network Policy & Free Chat

## 两个设计问题

1. **限制流量** — LLM 烧钱, 需要 quota 控制
2. **自由聊天** — 不只是任务驱动, 作者之间能自由讨论

---

## 问题 1: 流量控制

### 5 维度限流

| 维度 | 配置 | 作用 |
|---|---|---|
| Per-author mail | `max_mails_per_hour=30`, `max_mails_per_day=200` | 防单个 author 刷屏, 跟 OpenRouter free tier 对齐 |
| Per-tick action | `max_actions_per_tick=3` | 单 tick 内最多 3 个 action, 防模型暴走 |
| Per-thread round | `max_thread_rounds=8` | 同一 thread 最多 8 轮 Re:, 防死循环 |
| Tick cooldown | `min_tick_interval_seconds=3` | 同一 author 两次 tick 至少 3s, 防模型卡顿重试 |
| Thread idle | `thread_idle_close_seconds=600` | 10 分钟无活动自动 close |

### 实现

`src/agents_chat/policy.py`:
- `NetworkPolicy` (dataclass) — 全局配置
- `RateLimiter` (SQLite 后端) — per-author hour/day 桶
- 在 `Author._execute` 入口检查 + `_heartbeat_loop` 入口检查

### 可配置 (env)

```bash
AGENTCHAT_MAX_MAILS_PER_HOUR=30
AGENTCHAT_MAX_MAILS_PER_DAY=200
AGENTCHAT_MAX_ACTIONS_PER_TICK=3
AGENTCHAT_MAX_THREAD_ROUNDS=8
AGENTCHAT_MIN_TICK_INTERVAL=3
```

### Web UI 可见

Dashboard 卡片显示: `Mails (hour) 3 / 30`, `Mails (day) 3 / 200`.
到限额会标红。

---

## 问题 2: 自由聊天 (Free Chat)

### 机制

`FreeChatManager` (在 policy.py):
- `trigger(topic, started_by, authors)` — 开始一个 free chat session
- 所有 author **burst tick** (广播)
- 每次有人发邮件, `record_message(author, body)`, round +1
- **结束条件**:
  - round 达到 `free_chat_max_rounds` (默认 10)
  - idle 超 `free_chat_idle_seconds` (默认 120s)
  - 手动 `end()`

### 触发方式

**A. 手动 (Web UI)**:
```
Conversations tab → "🎙️ 触发自由聊天" 按钮 → 输入话题 → 广播
```

**B. 手动 (API)**:
```bash
curl -X POST http://localhost:7331/api/freechat \
  -H "Content-Type: application/json" \
  -d '{"topic":"架构 review","started_by":"god"}'
```

**C. 自动 (Phase 2)**:
- 周期触发 (e.g. 每周 1 次"团队周会")
- 触发条件 (e.g. 任务全部完成时)

### 跟普通 task 区别

| | Task 邮件 | Free Chat 邮件 |
|---|---|---|
| 触发 | 用户发任务 / PM 派活 | 手动 trigger (或周期) |
| thread | task-{id} | freechat-{session_id} |
| 作者 | 1 收件人 | **广播给所有 author** |
| 结束 | session 完成 | 轮数满 / idle 超时 |
| monitor 记录 | mail_sent | mail_sent (同) |

**架构上 Free Chat 也是邮件**, 复用全部 author/mailbox/monitor 机制。
只是 trigger 行为不同: **broadcast 而不是 targeted**。

---

## 整体架构 (含 policy + monitor)

```
┌────────────────┐
│  Author         │  ← 我们的核心
│  ┌────────────┐ │
│  │ Heartbeat  │──→ cooldown check ──→ NetworkPolicy.min_tick_interval
│  │ _tick()    │──→ rate limit check ──→ RateLimiter.check()
│  │ _execute() │──→ max_actions_per_tick ──→ NetworkPolicy.max_actions_per_tick
│  │   ├ mail   │──→ monitor.mail_sent() ──→ monitor.jsonl
│  │   └ tool   │──→ monitor.tool_used() ──→ monitor.jsonl
│  └────────────┘ │
└────────┬───────┘
         │  (status, ticks, actions, sessions)
         ↓
┌────────────────┐
│  Monitor        │  ← 观察
│  (jsonl log)    │  ← conversation 过滤
│  (SQLite stats) │  ← rate counts
└────────────────┘
         ↑
         │  (read-only)
         ↓
┌────────────────┐
│  Web UI         │  ← 可视化
│  - Dashboard    │  ← per-author status
│  - Conversations│  ← agent↔agent timeline + free chat + policy
│  - Mailbox      │  ← 全部邮件 (含 god)
└────────────────┘

         ↑
         │  (POST /api/freechat)
         ↓
   FreeChatManager.trigger() ──→ burst all authors
```

---

## 文件位置

- `src/agents_chat/policy.py` — NetworkPolicy / RateLimiter / FreeChatManager
- `src/agents_chat/author/base.py` — _heartbeat_loop + _execute 集成 policy
- `src/agents_chat/heartbeat.py` — Registry 加 free_chat + start_free_chat
- `src/agents_chat/main.py` — env 读 policy 配置
- `src/agents_chat/web/server.py` — /api/policy + /api/freechat
- `src/agents_chat/web/ui/index.html` — conversations tab 显示 policy + free chat

## 测试

11 个新 policy/rate_limiter/free_chat 测试.
**56/56 total unit tests pass.**

## Demo 验证

```
[2026-06-06 12:53:23] mail_received: pm ← god
[2026-06-06 12:53:29] mail_sent: pm → zhang-frontend
[2026-06-06 12:53:29] mail_received: zhang-frontend ← pm
[2026-06-06 12:53:52] mail_sent: pm → god
[2026-06-06 12:54:39] mail_sent: pm → zhang-frontend

rate limit DB: pm 3/30 hour, 3/200 day ✓
```
