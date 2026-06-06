# 05 — Qwen (via OpenRouter) Integration

## 现状

- **Qwen Code CLI 自带 OAuth 已停服** (2026-04-15)
- 还能用 Qwen 的方法: **OpenRouter free tier** (`qwen/qwen3-coder:free`)
  - 20 req/min, **200 req/day** 免费
  - 不需要信用卡, 1 分钟拿 key

## 拿到 OpenRouter key (1 分钟)

1. 去 https://openrouter.ai/ 注册 (Google 一键)
2. Settings → Keys → Create Key
3. 复制 key (格式: `sk-or-v1-...`)

## 配到我们的项目

```bash
# 1. 把 key 存到 env
export OPENROUTER_API_KEY="sk-or-v1-..."

# 2. 跑 demo (qwen 后端)
python -m agents_chat.main demo --llm qwen

# 3. 或 web
python -m agents_chat.main web --llm qwen
# 打开 http://localhost:7331

# 4. 选其他 model
python -m agents_chat.main demo --llm qwen --model qwen/qwen-2.5-coder-32b-instruct
```

## 架构

```
┌─────────────────┐
│  Author         │  (我们的抽象)
│  ┌───────────┐  │
│  │ Think     │──┼──→ QwenAgent (新)
│  └───────────┘  │         ↓
└─────────────────┘    HTTP POST /chat/completions
                            ↓
                  OpenRouter → Qwen3-Coder (Qwen team)
                            ↓
                       JSON 决策 → Decision
```

## 跟 OpenCode 的区别

| | OpenCode | QwenAgent |
|---|---|---|
| 协议 | CLI subprocess | HTTP API |
| 能力 | LLM + tools (bash/read/write) | LLM only (纯文本) |
| 启动 | 5-10s | ~2s |
| 改文件 | ✅ 真改 | ❌ 不能 (纯 LLM) |
| 免费 | opencode/* proxy 限速 | OpenRouter 200/day |
| Token 限速 | 未知 | 20 req/min |

**QwenAgent 只能发邮件, 不能改文件.** 这是因为它纯 LLM, 不会自己用工具。
要改文件需要 OpenCode (它有 agent loop + tools)。

## 三种 LLM backend 配合使用

```bash
# A. mock - 纯本地, 不花钱
python -m agents_chat.main demo --llm mock

# B. opencode - 真改文件, 但慢 + 模型不可控
python -m agents_chat.main demo --llm opencode --model ollama-cloud/qwen3-coder:480b

# C. qwen - 快, 免费 200/day, 但只能发邮件
python -m agents_chat.main demo --llm qwen
```

**最佳组合**:
- PM (需要决策 + 派活) → qwen (快)
- 工程师 (需要改文件) → opencode (有 tools)
- QA / Reviewer → qwen (快)

## 文件

- `src/agents_chat/llm/qwen.py` — QwenAgent 类
- `tests/unit/test_qwen.py` — 5 个测试 (mock HTTP)
- `docs/05-qwen-integration.md` — 本文档
