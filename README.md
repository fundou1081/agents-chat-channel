# agents-chat-channel

> Multi-author agent runtime. Each agent is a long-lived **Author** with its own
> inbox, multi-session memory, and autonomous heartbeat. No central orchestrator.

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
