# 18. v2.0 DecisionMaker (LLM 选 Session)

> Status: ✅ v2.0 新增 (370/370 tests 全过)
> Date: 2026-06-08
> 配套 commit: 即将

---

## 0. TL;DR

新组件 `DecisionMaker` 取代 `SessionManager.decide_session` 的纯程序化判定, 用 **1 次 LLM API call** 决定 session **续 / 新建 / skip**。

- **邮箱路径** (显式 @自己) → **必答** (二选一 continue/new)
- **轮询路径** (其他) → **LLM 三选一** (continue/new/skip)
- **fallback** → SessionManager.decide_session (纯程序化, 老的精确+topic 模糊)
- **用 openai 库** (干净解耦, 不调 CLI)

---

## 1. 为什么需要 DecisionMaker

### 老逻辑 (SessionManager.decide_session)

```python
def decide_session(task_id, topic, channel):
    # 1. 精确匹配: 同 (channel, task_id) 的 active session → 续
    # 2. 模糊匹配: 同 channel + topic 关键词 → 续
    # 3. 不命中 → 新建
```

**3 个问题**:
1. **不调 LLM** → 不结合 worker 角色 / 身份 / session 总结
2. **不感知 context** → 只看 task_id 精确 + topic 子串
3. **没 skip 选项** → 所有投递都必答 (没"忽略"机制)

### 新逻辑 (DecisionMaker)

```python
async def decide(mail, sessions, role, is_must_reply) -> Decision:
    # 1 次 LLM call
    # 看: role + sessions (主题/总结/下一步) + content
    # 输出: continue / new / skip
```

**改进**:
- ✅ LLM 决定, 结合 role + 完整 session 上下文
- ✅ 邮箱路径必答 (二选一)
- ✅ 轮询路径可 skip (三选一)
- ✅ Fallback 到纯程序化 (LLM 失败时)

---

## 2. 决策流程

```
Mail 到达 mailbox
       │
       ▼
EventHandler.handle_mail(mail)
       │
       │ 1. 读 path 字段 (Scanner 投递时填的)
       │    path = "email"  → 邮箱路径 → is_must_reply = True
       │    path = "poll"   → 轮询路径 → is_must_reply = False
       │    path = "broadcast" → 轮询路径
       │
       │ 2. INPUT GATE (filter content)
       │
       │ 3. DecisionMaker.decide()
       │    ├─ 构造 prompt: role + sessions + content
       │    ├─ 调 1 次 LLM (openai 库)
       │    ├─ 解析 JSON 输出
       │    └─ **强约束**:
       │       - 邮箱路径 + skip → 强制改 new
       │       - continue + session_id 无效 → 强制改 new
       │       - 非法 action → 强制改 new
       │       - LLM 失败 → 抛 → fallback SessionManager
       │
       │ 4. 根据 decision:
       │    - skip → 写 system "ignored", return
       │    - continue → 续 session
       │    - new → 新建 session
       │
       │ 5. _build_prompt + cli.execute (第 2 次 LLM, 生成 reply)
       │
       │ 6. OUTPUT GATE + write channel
```

**关键**: **2 次 LLM call per mail**:
- 第 1 次: DecisionMaker (短 prompt, ~500 tokens)
- 第 2 次: CLI (长 prompt, ~3000 tokens, 含 channel history)

---

## 3. API

### 3.1 DecisionConfig

```python
from agents_chat.v2.decision import DecisionConfig

cfg = DecisionConfig(
    base_url="https://api.openai.com/v1",  # 默认
    api_key="sk-xxx",                        # 默认从 env 读 MINIMAX_API_KEY
    model="gpt-4o-mini",                     # 默认
    temperature=0.0,                         # decide 要 deterministic
    timeout=10.0,                            # LLM call 超时
    max_retries=1,
)
cfg.is_valid()  # api_key 必填, 否则 False
```

**环境变量** (复用 CLI 配置):
- `MINIMAX_API_KEY` / `OPENAI_API_KEY` / `DECISION_API_KEY` → api_key
- `MINIMAX_BASE_URL` / `OPENAI_BASE_URL` / `DECISION_BASE_URL` → base_url
- `DECISION_MODEL` / `MINIMAX_DECISION_MODEL` → model

### 3.2 Decision

```python
@dataclass
class Decision:
    action: str            # "continue" | "new" | "skip"
    session_id: str = ""   # action=continue 时填
    reason: str = ""       # LLM 给的理由
    confidence: str = "medium"
    raw: str = ""          # LLM 原始返回 (调试用)
```

### 3.3 DecisionMaker

```python
from agents_chat.v2.decision import DecisionMaker, DecisionConfig

cfg = DecisionConfig(api_key="sk-xxx", model="gpt-4o-mini")
dm = DecisionMaker(cfg)

decision = await dm.decide(
    mail={"content": "@bot 续聊买鱼", "path": "email", "channel": "fish-market"},
    sessions=[
        {"session_id": "s1", "topic": "买鱼", "channel": "fish-market",
         "progress": 50, "content_summary": "谈到 80 元", "next_action": "等还价"},
    ],
    role="你是 buyer-fish, 你的职责是讨价还价",
    is_must_reply=True,  # 邮箱路径
)
# decision.action == "continue" or "new" or "skip"
```

### 3.4 集成到 EventHandler / Agent

```python
from agents_chat.v2.agent import Agent
from agents_chat.v2.decision import DecisionConfig, DecisionMaker

# 方式 1: 传 config (自动构造 DecisionMaker)
agent = Agent(
    agent_id="bot", cli=MockCLI(), data_dir="./data_v2",
    decision_config=DecisionConfig(api_key="sk-xxx"),
)

# 方式 2: 直接传 DecisionMaker (高级, 复用 client)
client = OpenAIClient(base_url="...", api_key="...")
dm = DecisionMaker(DecisionConfig(model="gpt-4o-mini"), client=client)
agent = Agent(agent_id="bot", cli=MockCLI(), data_dir="./data_v2", decision_maker=dm)

# 方式 3: 不传 (config 无效) → 自动 fallback SessionManager.decide_session
agent = Agent(agent_id="bot", cli=MockCLI(), data_dir="./data_v2")
```

---

## 4. Prompt 格式

### 4.1 邮箱路径 (is_must_reply=True)

```
[Worker 角色]
你是 buyer-fish, 你的职责是讨价还价

[现有 sessions (1 个 active)]
  - s1 | fish-market | prog=50% | topic='买鱼'
    summary: 谈到 80 元
    next_action: 等还价

[当前 mail]
path: email
channel: fish-market
task_id: 
content: @bot 续聊买鱼

[决定]
**必须回复** (这是显式 @你的消息).
请从 [continue / new] 中选一个, 决定用哪个 session 答:
  - continue: 用现有 session (填 session_id)
  - new: 新建 session

[输出格式 - 严格 JSON, 单行]
{"action": "continue", "session_id": "s_xxx", "reason": "..."}
或 {"action": "new", "reason": "..."}
或 {"action": "skip", "reason": "..."}
```

### 4.2 轮询路径 (is_must_reply=False)

```
[Worker 角色]
...

[现有 sessions ...]

[当前 mail]
path: poll
channel: general
content: 一些广播消息, 不一定 @你

[决定]
请从 [continue / new / skip] 中选一个:
  - continue: 用现有 session 续 (填 session_id)
  - new: 新建 session (新话题)
  - skip: 跟我无关, 忽略 (填 reason 简短解释)

[输出格式 - 严格 JSON, 单行]
...
```

---

## 5. 解析规则 (强约束)

| 输入 | 输出 | 原因 |
|------|------|------|
| LLM 返回 valid JSON `{action: continue, session_id: s1}` | `continue(s1)` | 正常 |
| LLM 返回 valid JSON `{action: new}` | `new()` | 正常 |
| LLM 返回 valid JSON `{action: skip}` + 邮箱路径 | `new()` | **强制必答**, 改 new |
| LLM 返回 valid JSON `{action: skip}` + 轮询路径 | `skip()` | 正常 |
| LLM 返回 `{action: continue, session_id: s_ghost}` (s_ghost 不存在) | `new()` | **session_id 无效**, 改 new |
| LLM 返回 `{action: continue}` (没填 session_id) | `new()` | 同上 |
| LLM 返回 `{action: maybe}` (非法) | `new()` | **非法 action**, 改 new |
| LLM 返回 "garbage" (无法解析) | 邮箱→`new()`, 轮询→`skip()` | fallback |
| LLM 抛异常 (timeout/network) | 抛 `Exception` | **不静默**, 让 EventHandler fallback |

**强约束目的**: 即使 LLM "出错", 邮箱路径也**保证不丢必答消息**。

---

## 6. Fallback 链

```
LLM 配置有效?
  ├─ 是 → DecisionMaker.decide() (1 次 LLM call)
  │       ├─ 成功 → 强约束解析 → continue/new/skip
  │       └─ 失败 → 抛 Exception
  │
  └─ 否 → 直接 fallback SessionManager.decide_session (纯程序化)

最终 fallback (LLM 抛异常时):
  → EventHandler 捕获异常 → fallback SessionManager.decide_session
  → 老逻辑: 精确 + topic 模糊 + 不命中新建 (无 skip 选项)
```

**为什么 fallback 不重要**: 大多数 LLM 都会正常返回 JSON, fallback 是**保险**, 不是常态。

---

## 7. Skip 行为

**Skip 时写 system 消息** (审计可追):

```json
{
  "from": "bot",
  "type": "system",
  "content": "[bot] ignored (DecisionMaker skip)\n\n<!--STATUS\n session_id: -\n task_id: task_xxx\n progress: 0\n summary: bot 决定忽略此 mail\n next_action: (无)\n confidence: low\n-->"
}
```

**Skip 触发条件** (轮询路径):
- LLM 判断 "跟我无关"
- LLM 输出无法解析
- 走 fallback 且 fail

**Skip 不触发** (邮箱路径):
- LLM 想 skip → 强制 new
- 必答承诺

---

## 8. 测试覆盖 (`tests/unit/v2/test_decision_maker.py`)

30 tests:
- `TestDecisionConfig` (4): 默认 / is_valid / env override
- `TestDecision` (4): 数据类 / to_dict
- `TestDecisionMakerDecide` (13): continue/new/skip 正常 + 邮箱路径强制 + 5 个 fallback + LLM 异常 + 文本嵌入 + prompt 内容
- `TestEventHandlerIntegration` (6): continue/new/skip/邮箱强制/fallback/无 DM
- `TestScannerPath` (2): mention 投 email / broadcast 投 broadcast

**总测试数**: 370/370 passed in 10.70s (从 340 → 370, +30)

---

## 9. 关键 commit (本批)

```
1. feat(decision): 新增 decision.py (DecisionMaker + DecisionConfig + Decision)
2. feat(scanner): _deliver_mail 投递时填 path (email | broadcast)
3. feat(event_handler): handle_mail 集成 DecisionMaker + _decide_session + _write_skip
4. feat(agent): Agent 加 decision_config / decision_maker 参数
5. test(decision): 30 tests 覆盖
6. docs: 18-decision-maker.md
```

---

## 10. 路线图 (DecisionMaker 之后)

- [ ] **WebUI 接入**: 看到 worker 的 decision history (LLM 决定 + reason)
- [ ] **多 model fallback**: gpt-4o-mini 失败 → claude-haiku → 纯程序化
- [ ] **prompt 优化**: 加 few-shot examples (续 vs new vs skip 各 1 例)
- [ ] **decision cache**: 同 (mail hash + sessions hash) → 复用上次 decision
- [ ] **decision 持久化**: data_v2/decisions/<agent>.json, 调试 / 训练用
