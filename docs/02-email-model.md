# 02 — Email Model

## 为什么是 Email, 不是消息队列?

我们用 SQLite + 简单的 deliver/fetch, 而**不是 RabbitMQ / Kafka / Redis Streams**。原因:

1. **简单**: 1000 行内能写完, 不需要起服务
2. **持久**: 邮件不丢, 重启可恢复
3. **可观察**: 用 SQL 就能查任何邮件流
4. **可扩展**: 后期换 Postgres / NATS, 接口不变
5. **作者可读**: Author 知道 "我有 3 封未读", 像看收件箱

## Mail 数据结构

```sql
CREATE TABLE mails (
    id TEXT PRIMARY KEY,
    sender TEXT NOT NULL,
    recipients TEXT NOT NULL,        -- JSON array
    thread_id TEXT NOT NULL,
    in_reply_to TEXT,
    subject TEXT,
    body TEXT,
    attachments TEXT,                -- JSON
    priority INTEGER,
    requires_ack INTEGER,
    created_at TEXT NOT NULL,
    read_at TEXT,
    acked_at TEXT,
    metadata TEXT,                   -- JSON
    delivered_at TEXT
);
```

**所有邮件存在一个共享 DB**。 不按 author 分库。 这样:
- 跨 author 查询简单 (上帝看所有邮件)
- 数据迁移简单
- 适合 MVP 阶段

后期如果要按 author 分库, 改 `MailboxDB(db_path, partition_key=author_id)` 即可。

## Pull vs Push

| 模式 | 优点 | 缺点 |
|------|------|------|
| **Push** (传统 MQ) | 实时, 收件人立刻收到 | sender/receiver 锁步, 收件人挂了消息丢 |
| **Pull** (Email) | 解耦, 批处理, 容错 | 延迟高 (最多 heartbeat interval) |
| **混合** (我们用) | push 触发 burst, pull 取数据 | 复杂度高一点 |

**具体**:

1. Sender 调 `await mailbox.deliver(mail)`, 邮件进 DB
2. 如果 recipient 是个 author, system **burst 触发**它的 `_new_mail_event`
3. Author 的 heartbeat loop 醒来, **pull 拉**新邮件
4. LLM 决策, **执行** (发邮件, 调工具, 等)

**延迟 = 0** (burst 触发), 但**幂等 + 容错** (邮件在 DB 里, 不丢)。

## 路由

**目前**:
- 一个 author 一个 mailbox
- 发邮件时 recipients 列表
- fetch_unread(owner) 拉自己收件箱的未读

**后期** (Phase 4+):
- 主题订阅 (subscribe("api.*"))
- 关键词路由
- 转发 (god 邮件自动 cc 给 PM)
- 群组 (group: "frontend-team" = [zhang, wang])

## Burst Trigger

发邮件时, sender 通过 `Author.registry` 找到 recipient, 调用 `recipient.trigger_immediate_tick()`:

```python
async def _execute(self, decision, new_mail):
    for m in decision.outgoing_mail:
        await self.mailbox.deliver(m)
        for r in m.recipients:
            if r != self.persona.id and self.registry:
                other = self.registry.get(r)
                if other:
                    other.trigger_immediate_tick()  # ← burst!
```

**好处**:
- 上帝发任务, recipient 立即处理 (不等下个 heartbeat)
- 作者之间通信实时 (不是 30s 延迟)

**注意**:
- `trigger_immediate_tick()` 只 set 一个 Event, 不直接调 _tick
- Author 的 _heartbeat_loop 看到 Event 被 set, 立即跳出 wait_for, 跑 tick
- 这样: 不会多个 tick 并发 (一个 author 一次只一个 tick)

## Failover

| 失败 | 行为 |
|------|------|
| Author 崩了 | 邮件还在 DB, 下次 start 起来继续 |
| LLM 调用超时 | tick 抛异常, status=stalled, 不影响其他 author |
| DB 锁了 | aiosqlite 自动 retry, 失败抛异常 |
| 网络挂 | 同上 |

**邮件不丢** 是 email model 最大的价值。

## 性能

**MVP 阶段 (单进程 + SQLite)**:
- 100 邮件/分钟 单 author 绰绰有余
- 10 author 并发跑 没事
- DB 写入 ~1ms/邮件

**后期优化** (Phase 3+):
- WAL 模式 SQLite
- Postgres
- 按 author 分 DB
- 加缓存 (内存中 unread 计数)
