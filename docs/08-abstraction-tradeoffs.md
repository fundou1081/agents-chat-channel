# 08 — Abstraction Trade-offs

> 反思我们设计的 11 个核心抽象: **每个的优缺点,以及为什么这么选**。
> 写于 Phase 4 完成后, 距离初始设计 12 个 commit 后。

---

## 目录

1. [Author — 长期活着的 agent](#1-author--长期活着的-agent)
2. [Mail — Email 作为消息协议](#2-mail--email-作为消息协议)
3. [Mailbox — SQLite 单库](#3-mailbox--sqlite-单库)
4. [Session — 多并行对话](#4-session--多并行对话)
5. [Heartbeat — 自主 tick](#5-heartbeat--自主-tick)
6. [Email Model — Pull 异步 vs Push](#6-email-model--pull-异步-vs-push)
7. [Per-Author Backend — 混合架构](#7-per-author-backend--混合架构)
8. [Recipient Routing — alias 解析](#8-recipient-routing--alias-解析)
9. [Monitor — 事件日志](#9-monitor--事件日志)
10. [Network Policy — 5 维限流](#10-network-policy--5-维限流)
11. [Free Chat — 广播讨论](#11-free-chat--广播讨论)
12. [整体架构层面](#整体架构层面)
13. [适用 vs 不适用场景](#适用-vs-不适用场景)
14. [反思: 做对了什么, 做错了什么](#反思-做对了什么-做错了什么)

---

## 1. Author — 长期活着的 agent

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **跨任务状态连续** | PM 记得昨天讨论过什么, zhang 记得上周 PR 改了哪些文件 |
| **多 session 并行** | 一个人同时干 3 件事, 符合人类工作模式 |
| **真自主** | 没人指挥, 它自己 tick 决策, 没有中央调度器单点 |
| **解耦生命周期** | 任务来/走不影响 author 存在 |
| **失败可恢复** | author 崩了邮件不丢, 下次启动继续 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **永远在跑, 占资源** | 3 author = 3 asyncio.Task + 3 heartbeat timer, 即使都没事 |
| **状态膨胀** | 长期跑后 sessions/activity_log 越来越大, 内存压力 |
| **进程崩了状态丢失** | tick 之间的 in-memory 状态 (self.sessions) 没持久化, 重启重置 |
| **debug 黑盒** | "为什么这个 author 在 t=15s 时 thinking?" 难以复现 |
| **heartbeat 浪费** | idle 时还在 polling, 只是没干啥 |
| **生命周期谁管?** | god 启动后, author 永远跑; PM 没人 close 它, 永远占位 |

**对比**: AutoGen Worker 启/停快但每任务冷启; 我们常驻但灵活。

---

## 2. Mail — Email 作为消息协议

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **通用协议** | 人类能读能写, debug 直接看 mailbox.db |
| **持久可重放** | 邮件不丢, 可以 replay, 支持 audit |
| **解耦 timing** | 发件人不知道收件人何时 tick, 完全异步 |
| **多收件人 + thread 免费** | 广播/讨论不用额外设计 |
| **priority + ack** | 重要任务能插队, 需要确认能标记 |
| **batch 处理** | tick 一次拉 N 封, LLM 看全貌 |
| **工具丰富** | SQLite, grep, 任何邮件工具都能用 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **延迟是 heartbeat 级的** | 不是实时, 默认 5-30s 延迟 (burst 后 < 1s) |
| **LLM cost 高** | tick 看到全部 active sessions, prompt 越来越长 |
| **mailbox 无限增长** | 1 个月 100K 邮件, SQLite 仍 OK 但查询变慢 |
| **没处理顺序保证** | 同一 thread 5 封邮件, 可能乱序处理 |
| **没 schema 强约束** | 邮件 body 是 free text, LLM 解析可能错 |
| **回声循环风险** | PM 回 zhang, zhang 回 PM, 需要 policy 限轮数 |

**对比**: 传统 message queue (RabbitMQ) 有 ack/retry/queue, 但太重; 我们是简化版。

---

## 3. Mailbox — SQLite 单库

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **持久** | 重启数据不丢, 这是 email model 最大的价值 |
| **可查询** | `SELECT * FROM mails WHERE thread_id=?` 一行 SQL |
| **索引** | recipient + read_at + created_at 复合索引够用 |
| **便宜** | SQLite 文件级, 不用起服务 |
| **单文件** | 备份 = `cp mailbox.db backup.db` |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **单进程锁** | SQLite 串行写, 高并发下成瓶颈 (10+ author 同时写) |
| **不分片** | 100 个 author 都在一个文件, 无法分库分表 |
| **JSON LIKE 查询丑** | `recipients LIKE '%"zhang"%'` 慢, 需要 full table scan |
| **不是 networked** | 跨机器跑不动, 要 NFS / 共享存储 |
| **没 WAL 优化** | 我们用了 aiosqlite 但没开 WAL, 可能写阻塞 |

**什么时候会出问题**: > 50 author 或 > 10K 邮件/小时。

---

## 4. Session — 多并行对话

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **人类化** | 真实员工同时跟 5 个人对 5 个事, 我们也一样 |
| **context 隔离** | 任务 A 和任务 B 的话题分开, LLM 不混 |
| **topic-level 粒度** | 比 conversation 更细, 比 turn 更粗, 刚刚好 |
| **status 状态机** | active/blocked/completed/stalled, 清晰 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **context 爆炸** | tick 看到 5 个 active session, prompt 长度失控 |
| **LLM 难处理多 session** | 模型本身不擅长在多 context 间切换 |
| **session 不关会泄露** | 必须靠 policy 限轮数 + 手动 close, 否则永久挂 |
| **"真"多任务 vs "假"多任务** | tick 一次只看 1 个 session 决策, 实际还是串行处理 |
| **session 之间的状态不可见** | 跟别的 session 共享什么? 不知道 |

**核心问题**: **我们是 1 个 author 真的并行, 还是 1 个 author 串行处理多个 session?**
答案是后者, 只是切换快看起来像并行。

---

## 5. Heartbeat — 自主 tick

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **真自主 agent** | 没人指挥, 它自己活着 |
| **无中央瓶颈** | 不像 orchestrator-driven, 有单点 |
| **burst 触发** | 实时性, 不等下个 interval |
| **可调间隔** | PM 15s / zhang 15s / li 18s, 可调 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **interval vs cost 矛盾** | 短 = 响应快但烧钱; 长 = 省但延迟 |
| **burst 级联** | PM 派活 zhang, zhang 回 PM, PM 又回 → 雪崩 (我们用 cooldown 限) |
| **空闲 tick 浪费** | 99% 时间 author 在 idle, 但 heartbeat 还在跑 |
| **asyncio overhead** | 每个 tick 一次 context switch, 100 author 可能成瓶颈 |
| **同步期望问题** | user 发任务, 期望"立刻", 实际要等 0-15s |

**根本问题**: heartbeat 是 polling, 本质是"代价换响应"。

---

## 6. Email Model — Pull 异步 vs Push

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **解耦 sender/receiver 生命周期** | 发件人崩了不影响收件 |
| **持久** | 邮件在 DB 里不丢 |
| **batch 处理** | tick 拉 N 封, LLM 看全 |
| **audit log** | 任何时刻能 replay |
| **可观察** | 邮件流就是真实故事 |
| **简单** | 不用起 MQ, SQLite 就够 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **pull 有延迟** | 收件人 0-15s 后才看到 (burst 立刻, 但仍要 LLM 决策) |
| **context window 压力** | 100 封邮件的 author 一次性 tick, prompt 太长 |
| **没真正实时** | 想"发完立刻收到回信"做不到, 要等 |
| **mailbox 单点** | 如果 SQLite 文件坏了, 全系统挂 |

**对比**: 真正实时场景 (客服) 我们不合适, 异步协作场景 (团队项目) 完美。

---

## 7. Per-Author Backend — 混合架构

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **人尽其才** | PM 快 (qwen), 工程师能改文件 (opencode) |
| **灵活** | 1 个 author 可换 backend 不影响其他 |
| **省钱** | 不是所有 author 都要 opencode (贵) |
| **可演进** | 新 backend (Claude/Gemini) 加到 menu 即可 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **3 个 backend = 3 倍复杂度** | 调试/维护成本 |
| **行为不一致** | qwen 决定"自己干", opencode 决定"真改文件" |
| **测试要分 3 套** | 改 1 个 backend, 其他 2 个也得测 |
| **模型不同 → prompt 难调** | 同 prompt 在 qwen 工作, 在 opencode 可能不工作 |
| **单点失败** | opencode proxy 死了, 工程师全挂 |
| **auth 风险** | qwen CLI OAuth 已死; OpenRouter 200/day 可能也死 |

---

## 8. Recipient Routing — alias 解析

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **健壮** | LLM 输出模糊 id, 自动 reroute |
| **简单** | 30 行 alias map + 模糊匹配, 够用 |
| **可演进** | 加新 alias 一行代码 |
| **dedup** | 同 1 个 id 多次出现自动去重 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **静默 drop** | LLM 写 "user-123" 这种, 被 drop, LLM 不知道 → 下次还发 |
| **模糊匹配可能错** | "zhang" 匹配 zhang-frontend, 可能也匹配未来 "zhang-researcher" |
| **alias 表要维护** | 加新 persona 时要更新 alias |
| **不能循环** | 同一 mail 经过 routing 还是 1 跳, 不能"中转" |
| **没 log 给 LLM** | LLM 不知道自己发错了 (应该 re-prompt 告诉它) |

**更好的方案应该是**: routing 失败时, **给 LLM 一个 retry hint**, 而不是静默 drop。

---

## 9. Monitor — 事件日志

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **可见** | 完整事件流, 实时看 |
| **debug 友好** | 任何 tick 之后看 jsonl 知道发生了什么 |
| **append-only** | 不会因写失败丢事件 |
| **轻量** | jsonl 比 SQLite 还轻 |
| **agent-only 过滤** | 真正聚焦"作者之间"的对话, 不被 god 噪声淹没 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **无限增长** | 没 compaction, 1 年后几百 GB |
| **读慢** | scan 整个文件, 没索引 |
| **不可查询** | 想 "zhang-frontend 上周发了多少封" 要 grep |
| **没 correlation ID** | 跨多 event 追踪一次任务难 |
| **没实时 alert** | "zhang 1 小时没 tick" 这种检测, 需要另外写 |

---

## 10. Network Policy — 5 维限流

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **成本可控** | 不会一天烧光 $100 token |
| **防暴走** | 模型卡顿时不会无限发邮件 |
| **防死循环** | thread max_rounds=8 阻断 Re: 链 |
| **公平** | per-author 配额, 不让一个 author 抢资源 |
| **可配** | 5 个数字全 env 变量 |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **magic numbers** | max_mails_per_hour=30 怎么定的? 拍脑袋 |
| **静默 throttle** | LLM 不知道它被限流了, 可能再发一遍仍被拒 |
| **不防 token waste** | 只算 mail 数量, 不算 token 数。 1 封 1k token 和 1 封 10k token 一样计 1 封 |
| **cooldown 与 heartbeat 冲突** | 外部 burst 触发 tick, 被 cooldown 挡掉 → 延迟 |
| **粒度粗** | 没法对"重要任务"放开限流, 只能全开或全关 |

---

## 11. Free Chat — 广播讨论

### ✅ 优点

| 优点 | 价值 |
|---|---|
| **真"团队"感** | 团队讨论, 有主持人有参与者 |
| **复用邮件机制** | 不需要新基础设施, 触发 broadcast 即可 |
| **自动结束** | 10 轮 / 120s idle 自动收场 |
| **状态可查** | `registry.free_chat_status()` 看 active session |

### ❌ 缺点

| 缺点 | 代价 |
|---|---|
| **没真 turn-taking** | 所有人同时被 burst, 但没有强制"轮流" |
| **spam 风险** | 10 轮 × 3 author = 30 封邮件, 几秒钟烧完 |
| **依赖 persona 主动性** | 如果 PM 没意愿参与, 自由聊天会冷场 |
| **没结构** | 真正讨论需要议题/议程/结论, 我们没有 |
| **消耗大** | LLM 一次 tick 看 4 个 active session + free chat 提示, prompt 长 |

---

## 整体架构层面

### ✅ 整体优点

| 优点 | 价值 |
|---|---|
| **简单** | 5 个核心抽象, 容易讲清楚 |
| **可观察** | 邮件 + monitor, 所有事都能看到 |
| **可扩展** | 加新 persona / backend / policy 都是加文件 |
| **真干活** | 不是 demo, 真的能跑 opencode 改文件 |
| **本地优先** | 不用起一堆服务, sqlite + python 就行 |

### ❌ 整体缺点

| 缺点 | 代价 |
|---|---|
| **复杂度 = N 个简单** | 1 个 author 简单, 3 个 author + 3 个 backend + policy + monitor + freechat = 实际复杂 |
| **单进程** | SQLite + asyncio 都在 1 个进程, 不能横向扩展 |
| **LLM cost 不可预测** | tick 频率 × session 数 × context 长度 = 烧钱速度, 难精确预测 |
| **没真多 agent 协作** | 实际上是 1 个 author + 1 个 LLM, 多个 author 只是多进程 |
| **没 human-in-loop 设计** | god 是观察者, 不能"审批" author 决定再继续 |
| **没事务** | tick 失败, 可能邮件发出但 session 没 close (状态不一致) |

---

## 适用 vs 不适用场景

### ✅ 适合用我们的架构

- **小团队 agent 协作** (3-10 author)
- **异步工作流** (写代码, review, 文档, 测试)
- **本地开发 / 实验** (单机跑)
- **多 LLM 混合** (不同任务用不同模型)
- **真干活, 不只是聊天** (改文件, 跑命令)

### ❌ 不适合

- **大规模生产** (> 50 author, > 10K 邮件/小时)
- **实时对话** (客服, 需要 < 100ms 响应)
- **强一致工作流** (金融交易, 需要事务)
- **跨机器** (需要分布式调度)
- **企业级合规** (需要审计/权限/加密)

---

## 反思: 做对了什么, 做错了什么

### ✅ 做对

1. **Author 抽象** — 比 AutoGen Worker 更实用, 符合人脑模型
2. **Email model** — 简单+持久+可观察, 赌对了
3. **Pull + burst** — 既异步又有实时
4. **Per-author backend** — 灵活性是真实需求
5. **Monitor** — 没它就盲飞
6. **Policy** — 防止"1000 封邮件把系统搞挂"

### ❌ 做错 / 可改进

1. **Routing 静默 drop** — 应该 re-prompt LLM
2. **Free chat 仍是 broadcast** — 不是真 turn-taking
3. **Session 太多 prompt 爆炸** — 应该给 session 排序/优先级
4. **没事务** — tick 失败状态可能不一致
5. **没 human approval** — LLM 错决定不能干预
6. **没 cost tracking** — 只知道烧, 不知道烧多少

---

## 核心 trade-off 一句话

> **我们赌的是**: 长期活着的、慢的、可观察的、可恢复的 agent, 胜过短命的、快的、难 debug 的 worker。
>
> **代价是**: 单进程, 小规模, LLM cost 高, 不能横向扩展。
>
> **但换来**: 真的"团队感", 邮件可读, bug 可追, 人能用。

**一句话**: **慢但真, 不是快但假**。
