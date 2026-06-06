# agents-chat-channel

> **v2.0** — Multi-agent runtime with file-bus (JSONL/JSON), pull-mailbox loop, and
> pure-program Scanner routing. 详见 [docs/11-v2-architecture.md](docs/11-v2-architecture.md).
>
> **v1.1** (deprecated, archived 2026-06-07) — 独立项目在
> [`~/my_proj/_deprecated/agents-chat-v1/`](../../_deprecated/agents-chat-v1/).
> 仍是 74 个回归 tests, 但不再维护.

---

# v2.0 (主版本)

> Multi-agent runtime. Each agent is a standalone process bound to an external
> CLI (qwen/opencode/mock). Communication via file bus (channels JSONL + per-agent
> mailboxes JSON + per-task locks). Pure-program Scanner routes messages, no central
> orchestrator, no LLM in the routing path.

## 核心抽象

| 概念 | 含义 | 类比 |
|------|------|------|
| **Author** | 持续活着的 agent,有 identity + heartbeat | 员工 / Actor |
| **Mail** | 异步消息,持久化在 mailbox | Email |
| **Mailbox** | 每个 author 一个收件箱(SQLite) | 邮箱 |
| **Session** | 一个 author 内部的多个并行对话 | 脑子里多个线程 |
| **Heartbeat** | 定时 tick,作者自己醒来处理 | 周期性检查邮件 |

**核心思想**: 没有中央 orchestrator,各 author 自主 tick 拉邮件、决策、行动。
这跟 AutoGen 的 "Worker 被 push 任务" 完全不同。

## 快速开始

```bash
# 安装
pip install -e .

# 跑 demo (2 个 author 互相发邮件)
python -m agents_chat.demo

# 启动 Web UI
python -m agents_chat.main web
# → http://localhost:7331
```

## 项目结构

```
src/agents_chat/
├── models.py          # Mail, Session, Author 数据类
├── storage/           # SQLite 持久化
│   ├── mailbox_db.py
│   └── session_db.py
├── author/            # Author 运行时
│   ├── base.py        # Author 基类
│   ├── heartbeat.py   # 心跳循环
│   └── think.py       # LLM 决策 (mock)
├── llm/               # LLM 适配
│   └── mock.py        # Mock LLM
├── web/               # FastAPI Web UI
│   ├── server.py
│   └── ui/
├── personas/          # 角色配置 (YAML)
└── main.py            # CLI 入口
```

## 路线图

- [x] Phase 1: 核心抽象 (Author/Mail/Mailbox/Session) + 2 author demo
- [ ] Phase 2: 接真实 LLM (Claude Code / OpenCode)
- [ ] Phase 3: DAG 并行调度 (Orchestrator author)
- [ ] Phase 4: Web UI 完善 (Gantt / 状态机 / 实时)
- [ ] Phase 5: Slack / Feishu bridge

## 设计文档

- [docs/01-author-abstraction.md](docs/01-author-abstraction.md) — 为什么是 Author 不是 Worker
- [docs/02-email-model.md](docs/02-email-model.md) — Email 邮箱模型
- [docs/03-autogen-comparison.md](docs/03-autogen-comparison.md) — 跟 AutoGen 的对比

## License

MIT
