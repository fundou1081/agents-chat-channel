# 19. Channel Subscription — Agent 主动订阅 + 自主发言

> Status: 方案设计
> Date: 2026-06-08

---

## 0. 核心概念

### Channel (频道)

跟现有 `Channel` (文件总线) 同名但不同语义:

| 旧语义 | 新语义 |
|--------|--------|
| 文件总线,消息存储 | **话题空间**, agent 订阅/参与 |
| agent 作为 member 加入 | agent 通过 subscription 加入 |
| @mention 触发被动响应 | **轮询 + DecisionMaker 触发主动发言** |

### Subscription (订阅)

每个 agent 有自己的订阅列表:

```python
agent.subscriptions = ["fish-market", "tech-news", "admin-alerts"]
```

订阅 = agent 主动**轮询**这些频道, 不是被动等投递。

---

## 1. 为什么需要订阅 vs 现有 @mention

### 现有架构问题

```
god @sell 开价  →  Scanner 检测 @mention  →  投递 mail (path=email)  →  seller 响应
```

**问题**: god 必须在场, agent 自己不会主动发起对话。

### 订阅架构

```
seller-fish 订阅 ["fish-market"]
buyer-fish 订阅 ["fish-market"]

seller-fish 轮询 fish-market:
  - 发现频道空 → DecisionMaker: "没内容,发起对话" → post "野生鲈鱼 30 块"
  
buyer-fish 轮询 fish-market:
  - 发现 seller 新消息 → DecisionMaker: "相关,应回复" → post "30 太贵, 70 卖不卖?"

seller-fish 轮询:
  - 发现 buyer 新消息 → DecisionMaker: "相关,继续谈" → post "80 就 80"
  
... 直到成交或超时
```

**改进**:
- ✅ 无需 god/用户发第一句话
- ✅ agent 自己决定什么时候说话 (DecisionMaker)
- ✅ 可以多 agent 实时互动 (不像 mail 串行)
- ✅ 更像真实群聊 (agent 主动参与)

---

## 2. 架构设计

### 2.1 核心组件

```
ProactiveEventHandler (新)
  ├─ subscriptions: list[str]       # agent 订阅的频道列表
  ├─ _poll_subscriptions()         # 轮询所有订阅频道
  ├─ _decide_to_speak()            # DecisionMaker: 要不要发言
  ├─ _generate_reply()             # 调 CLI 生成回复
  └─ _post_to_channel()            # 写频道

vs 旧 EventHandler (被动模式):
  ├─ _poll_mailbox()               # 轮询 mailbox (等待 mail)
  ├─ _decide_session()             # DecisionMaker: session 续/新/skip
  ├─ _build_prompt()               # 构造 prompt
  ├─ _execute_cli()                # 调 CLI
  └─ _write_channel()              # 写频道
```

### 2.2 两种运行模式

```python
class EventHandler:
    """支持两种模式:"""
    
    # 被动模式 (现有): 等 mail 事件
    async def run_passive(self):
        async for event_type, event_data in self.comms.listen():
            if event_type == "mail":
                await self.handle_mail(event_data)
    
    # 主动模式 (新): 订阅频道 + 主动轮询
    async def run_proactive(self):
        while not self._stop_event.is_set():
            for channel in self.subscriptions:
                new_messages = await self._poll_channel(channel)
                if new_messages:
                    decision = await self._decide_to_speak(channel, new_messages)
                    if decision.should_speak:
                        reply = await self._generate_reply(channel, new_messages, decision)
                        await self._post_to_channel(channel, reply)
            await asyncio.sleep(self.poll_interval)
```

### 2.3 DecisionMaker 两种 decision 类型

```python
# 被动模式 decision (已有)
Decision(action="continue" | "new" | "skip", session_id, reason)

# 主动模式 decision (新)
@dataclass
class ProactiveDecision:
    should_speak: bool          # 要不要发言
    speak_now: bool             # 立刻说,还是等下一轮
    session_id: str = ""        # 用哪个 session
    topic: str = ""             # 新话题
    reason: str = ""            # 理由
    style: str = "reply"        # "reply" | "initiate" | "summarize" | "ignore"
```

### 2.4 Agent 初始化两种模式

```python
class Agent:
    def __init__(self,
        subscriptions: list[str] | None = None,   # 主动模式: 订阅频道列表
        mode: str = "passive",                   # "passive" | "proactive"
        poll_interval: float = 5.0,             # 主动模式轮询间隔
        proactive_config: ProactiveConfig | None = None,
        ...
    )
```

---

## 3. 文件改动

### 3.1 新文件

| 文件 | 内容 |
|------|------|
| `src/agents_chat/v2/core/event_handler.py` | EventHandler (passive + proactive 模式都内含) |
| `src/agents_chat/v2/core/decision.py` | DecisionMaker (含 `decide_speak` 主动模式逻辑) |
| `tests/unit/runtime/test_event_handler.py` | 主动模式测试 |
| `examples/e2e_autonomous.sh` | 无 god 的自主 e2e 脚本 |

### 3.2 改动文件

| 文件 | 改动 |
|------|------|
| `src/agents_chat/v2/core/event_handler.py` | 加 `subscriptions`, `run_proactive()` 方法 |
| `agent.py` | 加 `subscriptions`, `mode` 参数 |
| `decision.py` | 加 `ProactiveDecision` + `ProactiveDecisionMaker` |
| `files/channel.py` | 加 `read_new(from_offset)` 方法 |

### 3.3 废弃/重构

| 组件 | 改动 |
|------|------|
| `Scanner` | 主动模式下**不需要** Scanner (agent 自己轮询频道) |
| `mailbox` | 主动模式下**不需要** mailbox |
| `CommunicationComponent` | 主动模式下**不需要** (agent 直接读频道文件) |

---

## 4. 被动 vs 主动模式对比

| 维度 | 被动模式 | 主动模式 |
|------|----------|----------|
| **触发** | Scanner 检测 @mention → mail | agent 轮询订阅频道 |
| **发起** | 必须有外部消息 (@mention) | agent 自己可以发起 |
| **依赖** | Scanner + mailbox | 只需要频道文件 |
| **适用** | 人机混合, god 控制节奏 | 全自主 agent 社交 |
| **DecisionMaker** | decide_session (续/新/skip) | decide_speak (要不要说/说什么) |
| **mail path** | email / poll / broadcast | 无 (不看 path) |

---

## 5. ProactiveDecisionMaker prompt 设计

### 5.1 场景: 频道新消息,决定要不要发言

```
[Worker 角色]
你是 buyer-fish, 你的职责是讨价还价买鱼.

[频道 fish-market 最近消息]
  [10:00] seller-fish: "野生鲈鱼 30 块一斤"
  [10:01] 你 (buyer-fish): "30 太贵, 70 卖不卖?"
  [10:02] seller-fish: "80 就 80"

[你当前的 session]
  session_id: s1, topic: 买鱼, progress: 50
  next_action: 等 seller 回应

[决定]
请判断: 你现在应该发言吗? 
  - 应该发言: 你有新的有效信息要传达 (还价/接受/追问)
  - 不发言: 对方已经接受/成交,或跟你无关

[输出格式 - 严格 JSON]
{
  "should_speak": true | false,
  "speak_now": true | false,       // should_speak=true 时
  "style": "reply" | "initiate" | "accept" | "ignore",
  "reason": "...",
  "confidence": "high" | "medium" | "low"
}
```

### 5.2 场景: 频道空,决定要不要主动发起

```
[频道 fish-market 状态]
  - 无消息 (冷启动)

[你当前的 session]
  - 无 active session

[决定]
请判断: 你应该主动发起对话吗?
  - 发起: 你有明确目标,频道适合发起
  - 不发起: 没准备好,或频道不适合

[输出格式 - 严格 JSON]
{
  "should_speak": true | false,
  "style": "initiate" | "wait",
  "topic": "买鱼",        // style=initiate 时填
  "reason": "...",
  "confidence": "high" | "medium" | "low"
}
```

---

## 6. e2e 自主脚本设计

```bash
#!/bin/bash
# e2e_autonomous.sh — 无 god, agent 自己决定对话

# seller-fish: 订阅 fish-market, 主动发起
# buyer-fish: 订阅 fish-market, 被动响应

# seller-fish 启动时:
#   - 订阅 ["fish-market"]
#   - run_proactive()
#   - 冷启动 → DecisionMaker: "initiate" → post "野生鲈鱼 30 块"

# buyer-fish 启动时:
#   - 订阅 ["fish-market"]
#   - run_proactive()
#   - 检测到 seller 新消息 → DecisionMaker: "reply" → post "30 太贵, 70"

# seller-fish 检测到 buyer 新消息 → DecisionMaker: "reply" → post "80 就 80"

# buyer-fish 检测到 seller 新消息 → DecisionMaker: "accept" → post "成交!"
```

---

## 7. 实现计划

### Phase 1: ProactiveEventHandler (约 2h)
- [ ] 新文件 `proactive_handler.py` (继承/复用 EventHandler)
- [ ] 加 `subscriptions` + `run_proactive()` 方法
- [ ] `_poll_channel()` 读频道新消息 (from_offset)
- [ ] 加 `ProactiveDecision` dataclass
- [ ] 加 `ProactiveDecisionMaker` (复用 DecisionMaker client)

### Phase 2: Agent 集成 (约 1h)
- [ ] Agent 加 `subscriptions` + `mode` 参数
- [ ] Agent.run() 根据 mode 选 run_passive() / run_proactive()
- [ ] 修 opencode.md 引导 (加频道订阅说明)

### Phase 3: 测试 + e2e (约 1h)
- [ ] `test_proactive_handler.py` (20 tests)
- [ ] `e2e_autonomous.sh` (无 god)
- [ ] 全量测试 390/390

### Phase 4: DecisionMaker Proactive (约 1h)
- [ ] ProactiveDecisionMaker.decide_speak() prompt
- [ ] 冷启动 initiate 决策
- [ ] reply/accept/ignore 决策

---

## 8. 关键设计决策

### Q1: agent 能看到其他 agent 的消息吗?

**A**: 能。频道对所有 member 可见, agent 轮询读频道文件,看到所有消息。

### Q2: 两个 agent 同时决定发言,写频道顺序?

**A**: 按时间戳顺序,channel.jsonl 是追加的。DecisionMaker delay 会造成轻微竞争,但不影响语义正确性。

### Q3: subscription 怎么配置?

```python
# 方式 1: Agent 构造时
agent = Agent(
    agent_id="seller-fish",
    subscriptions=["fish-market", "admin-alerts"],
    mode="proactive",
)

# 方式 2: opencode.md 里声明
# channel subscriptions:
#   - fish-market
#   - tech-news
```

### Q4: 主动模式下 DecisionMaker 调几次?

**每条新消息**: 1 次 decide_speak (要不要发言) + 1 次 CLI (生成内容) = **2 次 LLM call/条消息**, 跟被动模式一样。

### Q5: Scanner 在主动模式下还要吗?

**不要**。Scanner 的投递逻辑 (mention → mail) 在主动模式下不适用, agent 直接读频道。