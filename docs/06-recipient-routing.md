# 06 — Recipient Routing & Alias Resolution

## 问题

LLM (尤其小模型) 经常用模糊 id 当 recipient:
- "dev" / "developer" / "engineer" — 不是真实 author id
- "team" / "everyone" / "all" — 不知道派给谁
- "前端" / "后端" — 中文 (跟 id 不一致)
- "小张" / "小李" — display_name (跟 id 不一致)

如果不处理, 这些邮件会进 mailbox 但**没 author 能 tick 处理**, 沉了。

## 解法 (A + B)

### A. PM prompt 加 few-shot examples

PM 的 system_prompt 包含 3 个完整示例, 每个示例都显式用 `recipients: ["zhang-frontend"]` / `["li-backend"]`, 强调"严格用真实 id"。

### B. Author 层加 alias + fuzzy resolution

`src/agents_chat/author/routing.py` 提供 `resolve_recipients()`:
1. **精确匹配** — 已经是真实 id
2. **Alias map** — 30+ 常见别名映射
3. **模糊匹配** — 子串/前缀/display_name
4. **Drop** — 实在找不到, log warning

## Alias 表

```python
RECIPIENT_ALIASES = {
    # 通用工程师
    "dev": "zhang-frontend",
    "developer": "zhang-frontend",
    "engineer": "zhang-frontend",
    "coder": "zhang-frontend",
    "team": "pm",
    "everyone": "pm",
    "all": "pm",

    # 前端
    "frontend": "zhang-frontend",
    "front-end": "zhang-frontend",
    "fe": "zhang-frontend",
    "ui": "zhang-frontend",
    "前端": "zhang-frontend",
    "前端工程师": "zhang-frontend",
    "小张": "zhang-frontend",
    "zhang": "zhang-frontend",

    # 后端
    "backend": "li-backend",
    "back-end": "li-backend",
    "be": "li-backend",
    "api": "li-backend",
    "后端": "li-backend",
    "后端工程师": "li-backend",
    "小李": "li-backend",
    "li": "li-backend",

    # PM
    "manager": "pm",
    "pm": "pm",
    "经理": "pm",
    "林经理": "pm",

    # god (保留)
    "god": "god",
}
```

## Demo 实测

**PM prompt (A) 工作**: 直接用 `zhang-frontend`, 不再用 "dev"。

**Routing (B) 兜底**: 即使 LLM 用了 "dev", 也自动 reroute 到 zhang-frontend。

```
[pm] → mail to ['zhang-frontend']: [任务] 写一个 hello.py
[pm] → mail to ['god']: 关于任务 [694f8fcc] 写 hello.py
```

## 测试

13 个新单元测试:
- 精确匹配
- alias 映射 (dev/developer/team/前端/后端/小张/小李)
- 模糊匹配 (子串/display_name)
- 未知 drop
- dedup
- god 保留

35/35 unit tests pass total.

## Backend 能力 vs Routing

| Backend | 改文件? | 跑命令? | Routing 需要? |
|---|---|---|---|
| mock | ❌ | ❌ | ❌ (人写好) |
| opencode | ✅ | ✅ | ✅ |
| qwen (本地) | ❌ | ❌ | ✅ |

zhang 用 qwen backend 时, 收到任务会**主动回复"我没有 write 工具"** (这是合理行为)。
zhang 用 opencode backend 时, 真的能改文件。

**给不同 author 配不同 backend 是下个 phase 的事**。
