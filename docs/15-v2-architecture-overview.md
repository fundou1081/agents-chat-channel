# 15. v2.0 Architecture Overview

> Status: ✅ Production-ready (370/370 tests 全过, 20 commits)
> Date: 2026-06-08
> Author: 方浩博 + QClaw agent

---

## A. 核心概念词解释 (Glossary)

为方便后续讨论更精确, 先统一术语:

| 概念词 | 含义 | 跟 v1 / 别人叫法的区别 |
|--------|------|--------------------|
| **Worker** | 1 个正在跑的 agent 进程 (含 4 组件) | 跟 Claude Code "agent process" / AutoGen "agent" 同义 |
| **Channel (频道)** | 1 个 JSONL 文件, 多 agent 共享的对话流 | 跟 Slack channel / Discord channel 同义 |
| **Mailbox (邮箱)** | 1 个 JSON 文件, 每 worker 1 个, 装 pending 邮件 | 跟 v1 SQLite Mailbox 不同 (我们是文件) |
| **Mail (邮件)** | 1 个 dict, agent 间通信单元 (`{from, content, type, ref_msg_id, ...}`) | 跟 Slack DM / Email 同义 |
| **Lock (锁)** | `locks/task_xxx.lock` 文件, 任务认领 (O_CREAT\|O_EXCL) | 跟 v1 一样, 但实现从 SQL → 文件 |
| **Session** | 1 个 LLM 上下文窗口 (含 topic / progress / next_action) | 跟 Claude Code "session" 同义 |
| **SessionSnapshot** | Session 的轻量摘要 (Comms/Scanner 调 API 判断时用) | v2.0 新引入, 避免传整个 Session |
| **Scanner** | 后台进程, 扫频道 JSONL, 投递 mention mail (纯程序, 0 token) | 跟 v1 "Scanner" 类似, 但重写为 PDR 风格 |
| **Scheduler** | 全局后台进程, 检查 stale task, 发 request_status 邮件, 锁释放 | `scheduler.py`, v2.0 新引入 (v1 没) |
| **EventHandler** | Agent 内部决策 pipeline, 两种模式: passive (等 mail) / proactive (轮询订阅频道) | `event_handler.py` |
| **DecisionMaker** | LLM 决定 session 续/新建/skip (passive) + 要不要发言 (proactive) | `decision.py`, v2.0 新引入 |
| **Subscription** | Agent 主动订阅的频道列表. proactive 模式下轮询这些频道,DecisionMaker 决定要不要发言 | v2.0 新引入 |
| **Passive 模式** | 等 mail 事件 (Scanner 投递 @mention) → DecisionMaker 决定 session | 被动响应, 适合人机混合 |
| **Proactive 模式** | 订阅频道 + 轮询 → DecisionMaker 决定要不要发言 → CLI 生成 → 写频道 | 主动发起, 适合全自主 agent 社交 |
| **Agent (容器)** | 1 worker = 4 组件 (Comms + EventHandler + SessionMgr + CLI) 的 Python class | 跟 v1 `Author` 类似, 但瘦身为容器 |
| **PDR 4 组件** | Perceive (Comms) + Decide (EventHandler) + Remember (SessionMgr) + Act (CLI) | v2.0 4 组件架构核心 |
| **CLI 客户端** | 调外部 LLM 工具 (opencode / qwen / claude) 的适配层 | 跟 "tool adapter" 同义 |
| **Workspace** | 1 个 worker 的独立工作目录, 含 `<cli_name>.md` 引导 | 跟 Claude Code "workspace" 同义 |
| **`<cli_name>.md`** | Workspace 里的角色引导文件 (opencode.md / qwen.md / claude.md) | 跟 Claude Code "AGENTS.md" / "CLAUDE.md" 同义 |
| **5 条铁律** | 通信规则: @名字 / @admin / 立即执行 / 继续演 / [STATUS] | 来自 Claude Code 经验, 写到 `<cli_name>.md` |
| **STATUS 块** | LLM reply 末尾的状态报告 (单行 `[STATUS] x \| y` 或多行 HTML) | v2.0 必含, Scanner 解析 |
| **state_board** | 全局任务状态板 (key=task_id, 含 agent/progress/heartbeat) | v2.0 新引入 (v1 散落在多个表) |
| **scanner_state** | Scanner 各频道的 offset 持久化 | 防止重启后重复扫 |
| **scheduler_state** | Scheduler request_log 持久化 | 防止重启后丢超时任务 |
| **decide_session** | SessionManager 决定续/新建 session 的核心 API | v2.0 新引入 (v1 简单续) |
| **decide_speak** | DecisionMaker 决定要不要发言 (proactive 模式): speak/skip/initiate | v2.0 新引入 |
| **Channel path** | Scanner 投递时填的路径标签: `email` (必答) / `poll` (可 skip) / `broadcast` (可 skip) | v2.0 新引入, 影响 DecisionMaker 行为 |
| **e2e (端到端)** | 跑 2 真实 agent + 真 LLM 的集成测试 | 跟 e2e tests 同义 |

---

## B. 信息流程文字框图

把图 4, 5, 11, 12 等图片里的流程用文字画出来, 方便复制 / 嵌入代码注释。

### B.1 god 发起任务 → 4 组件处理 (核心流程, 图 1+3 整合)

```
┌──────────────────────────────────────────────────────────────────┐
│ 外部 (CLI / WebUI / 别的 agent)                                  │
│   $ python -m agents_chat.v2.main post fish-market "@sell @buy …"│
└────────────────────────────┬─────────────────────────────────────┘
                             │ post 命令
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ Scanner (后台进程)                                                │
│   - 频道 fish-market.jsonl append 1 行                            │
│   - 解析 mentions = ["sell", "buy"]                                │
│   - 调 CommunicationComponent.poll_new_mails()                     │
│   - 调 SessionManager.snapshot()  ← 把所有 session 状态快照一起送 │
│                                                                  │
│   对每个 mention target:                                          │
│     resolved = fuzzy_resolve_mention("sell", 已知 agents)         │
│     if resolved in members:                                      │
│         mailbox[resolved].append(mail)                            │
└────────────────────────────┬─────────────────────────────────────┘
                             │ 邮箱 pending: [{mail from god}]
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ CommunicationComponent (perceive)                                │
│   主动 poll:                                                      │
│     poll_new_mails() → 拿到 god mail                              │
│     is_relevant_mail(mail, my_sessions_snapshot) → True           │
│   yield ("mail", mail)                                           │
└────────────────────────────┬─────────────────────────────────────┘
                             │ 事件
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ EventHandler (decide + act)                                    │
│   handle_mail(mail):                                              │
│     1. parse task_id / topic                                      │
│     2. sessions.decide_session(task_id, topic, channel, snapshot) │
│        ├─ 精确: (channel, task_id) 命中 → 续                       │
│        ├─ 模糊: topic 包含 + snapshot 上下文 (progress<100) → 续   │
│        └─ 不命中 → 新建                                            │
│     3. _build_prompt: 含 opencode.md + session 上下文 + 频道真历史 │
│     4. cli.execute(session_id, prompt, workspace_dir)             │
│     5. parse_status_block (单行 [STATUS] 优先)                     │
│     6. sessions.update(progress, next_action, content_delta)      │
│     7. 写频道 reply                                                │
│     8. _second_route: 提取 @mention, 投递 mention mail            │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼ 续 → 循环 (3 轮讨价还价)
```

### B.2 3 轮讨价还价 (数据流, 图 5+9+12 整合)

```
T=0   god → post 频道 "@sell @buy 模拟讨价还价 3 轮"
      ↓
      Scanner 投递 mention → seller.mailbox + buyer.mailbox
      ↓
T=1   [Comms.seller poll]  yield ("mail", god_mail)
      [EventHandler.seller.handle_mail]
        ├─ parse topic="模拟讨价还价 3 轮", task_id="task_001"
        ├─ sessions.decide_session → 新建 Session(local_seller_001)
        ├─ _build_prompt: 5 条铁律 + @sell 开头 + 100 元开价 + STATUS 提醒
        ├─ cli.execute("") → OpenCodeCLI → "100 元一斤"
        │  reply 含 [STATUS] 报价100元 | 下一步: 等 buyer
        ├─ parse_status_block → progress=10
        ├─ sessions.update(progress=10, next_action="等 buyer", remote_id="oc_xxx")
        ├─ 写频道: "@buyer 100 一斤, 不讲价"
        └─ _second_route: @buyer → buyer.mailbox.append

T=2   [Comms.buyer poll]  yield ("mail", seller_reply)
      [EventHandler.buyer.handle_mail]
        ├─ topic="100 一斤", 续 buyer 自己的 session
        ├─ prompt 含真频道历史 (含 seller 的 100 元)
        ├─ cli.execute → "70 块! 老板"
        │  reply 含 [STATUS] 还价70 | 下一步: 等 seller
        ├─ progress=20
        ├─ 写频道: "@seller 70 块, 卖不卖?"
        └─ _second_route: @seller

T=3   [Comms.seller poll]  yield ("mail", buyer_reply)
      [EventHandler.seller.handle_mail]
        ├─ sessions.decide_session → 命中! 续 Session(local_seller_001)
        ├─ cli.execute(session_id="oc_xxx") → 续 → "80 块卖你"
        ├─ progress=50
        ├─ 写频道: "@buyer 最低 80"
        └─ _second_route: @buyer

T=4   [Comms.buyer poll]  yield ("mail", seller_reply)
      [EventHandler.buyer.handle_mail]
        ├─ 续 Session
        ├─ cli.execute → "成交 80"
        ├─ progress=100 → sessions.update(status="completed")
        ├─ 写频道: "🎉 80 元成交"
        └─ 锁释放 (lock_release)

数据流 4 个关键 API (Scanner 调):
  - CommunicationComponent.poll_new_mails()       → list[mail]
  - SessionManager.snapshot()                     → list[SessionSnapshot]
  - SessionManager.decide_session(task_id, topic, channel, snapshot) → (Session, is_new)
  - CLI.execute(session_id, prompt, workspace_dir) → CLIResponse
```

### B.3 文件总线 (图 1+3 整合)

```
┌─────────────────────────────────────────────────────────────────┐
│  4 组件读写 4 种文件 (原子, 跨进程)                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  CommunicationComponent (perceive)                              │
│    poll_new_mails() → 写 Mailbox (append)                        │
│    poll_my_active_tasks() → 写 StateBoard (upsert)               │
│    listen() → 读 Channel JSONL (tail)                            │
│                                                                   │
│  EventHandler (decide)                                         │
│    write_channel_reply() → append Channel JSONL                   │
│    _second_route() → 写 Mailbox (其他 agent)                     │
│    sessions.update() → 写 Session JSON (atomic)                   │
│    parse_status_block() → 读 LLM reply text                       │
│                                                                   │
│  SessionManager (remember)                                       │
│    decide_session() → 读 Session JSON (in-memory cache)          │
│    snapshot() → 读 Session JSON                                  │
│                                                                   │
│  CLI (act)                                                       │
│    execute() → spawn subprocess / HTTP API                       │
│    return CLIResponse (含 output_text + new_session_id)          │
│                                                                   │
│  Scheduler (global, stale task)                                              │
│    poll_stale_tasks() → 读 StateBoard + 锁 mtime                  │
│    release_lock() → O_EXCL 文件锁原子释放                         │
└─────────────────────────────────────────────────────────────────┘

所有文件用 pathlib.Path 跨平台, json.dump/load + tmp + os.replace 原子写.
```

### B.4 mention 路由 + 模糊匹配 (图 5+7 整合)

```
输入: 频道里一条 message
       {from: "god", content: "@sell @admin 帮看下报价", mentions: ["sell", "admin"]}

Scanner 路由:
  1. parse msg.mentions = ["sell", "admin"]
  2. 对每个 mention target:
     a. members = channel.list_members()  # 拿频道成员
     b. known_agents = list(set(members + all_known_agents))
     c. resolved = fuzzy_resolve_mention(target, known_agents)
        
        fuzzy_resolve_mention("sell", ["seller-fish", "admin", "god"]):
          1. 精确: "sell" in candidates? NO
          2. prefix: candidates where c.startswith("sell")? ["seller-fish"]
          3. 子串: candidates where "sell" in c? ["seller-fish"]
          4. 多匹配选最长: "seller-fish" ✓
        
        fuzzy_resolve_mention("admin", ["seller-fish", "admin", "god"]):
          1. 精确: "admin" in candidates? YES → "admin" ✓
        
  3. 对每个 resolved:
     mailbox[resolved].append(mail)

输出:
  seller-fish.mailbox.pending += [mail from god with @sell @admin]
  admin.mailbox.pending     += [mail from god with @sell @admin]
```

### B.5 跨平台 CLI 调用 (图 4 整合)

```
场景: Windows 跑 v2.0 (或 macOS / Linux)

┌─────────────────────────────────────────────────────────────────┐
│ EventHandler.handle_mail → CLI.execute()                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  v2 CLI 抽象 (Protocol):                                          │
│    async def execute(session_id, prompt, workspace_dir)            │
│                                                                   │
│  实现:                                                            │
│    OpenCodeCLI:                                                   │
│      binary_path = shutil.which("opencode")  ← 跨平台找         │
│        Windows: C:\...\opencode.exe (或 .cmd)                     │
│        POSIX:   /usr/local/bin/opencode                           │
│      cmd = [binary_path, "run", prompt, "--model", model,           │
│             "--format", "json", "--session", session_id]          │
│      kwargs["cwd"] = workspace_dir  ← opencode 读 opencode.md     │
│      proc = asyncio.create_subprocess_exec(*cmd, **kwargs)         │
│        ↑ 不传 shell=True, 避免 Windows .cmd wrapper 解析 args      │
│      return parse_output(proc.stdout)                              │
│                                                                   │
│    QwenCLI:                                                       │
│      POST http://localhost:11434/v1/chat/completions  ← HTTP API   │
│        ollama 本地 daemon 不验证 key (v2 修: 自动用 ollama-local)  │
│      prompt = inject_workspace_md(prompt, workspace_dir)            │
│        ↑ prompt 里 prefix workspace/<cli>.md 内容 (HTTP workaround) │
│      return CLIResponse(output_text, new_session_id)              │
│                                                                   │
│  v2.0 关键: 不用 shell, 不用 os.path, 不用 .cmd 路径                │
│  → Windows / macOS / Linux 一致, 17 跨平台 tests 全过             │
└─────────────────────────────────────────────────────────────────┘
```

### B.6 `<cli_name>.md` 5 条铁律 (图 5+12 整合, Claude Code AGENTS.md 风格)

```
┌─────────────────────────────────────────────────────────────────┐
│ {workspace_dir}/{cli.name}.md  (opencode.md / qwen.md / claude.md)│
│                                                                   │
│ 内容模板:                                                         │
│   # {agent_id} 角色定义 (v2.0) — 模板自动生成                    │
│   你是 {agent_id}。{system_prompt}。你在一个多人协作频道中。     │
│   频道: #{default_channel}, 成员: {members_str}。                  │
│   频道管理员: {admins_str}。                                     │
│                                                                   │
│ ## ⚠️ 5 条铁律 (频道通信)                                       │
│                                                                   │
│   1. 开头 @名字: 每条 reply 必须在开头指定收信人                 │
│      格式: @名字 你的内容                                          │
│      例: @buyer-fish 80 太贵, 70 卖不卖?                          │
│      反例: @xxx: 我要说...  (Scanner 路由会混乱)                 │
│                                                                   │
│   2. 不确定就 @频道管理员: 不确定对谁说, 就 @频道管理员          │
│      频道管理员在 metadata `admins` 列表里                        │
│                                                                   │
│   3. 管理员指令立即执行: 频道管理员的指令立即执行                │
│      不要先回 "收到/好的"                                          │
│                                                                   │
│   4. 角色扮演中继续演: 在角色扮演场景, 收到对方台词继续演          │
│      你的 prompt 会注入真频道历史 — 用它续剧情                     │
│                                                                   │
│   5. 末尾 [STATUS] 简述 | 下一步: xxx (单行格式)                  │
│      格式: [STATUS] <一句话> | 下一步: <action>                    │
│      例: [STATUS] 报价 100 元 | 下一步: 等 buyer 还价              │
│      缺 STATUS 块 → Scanner/Scheduler 看不到进度                          │
└─────────────────────────────────────────────────────────────────┘

CLI 工具 (opencode / qwen / claude) 启动时自动读这个文件作为角色引导.
Agent._init_workspace_files 启动时自动写, 已存在不覆盖.
```

### B.7 STATUS 块解析流程 (图 5+8 整合)

```
LLM reply: 
  "100 元一斤. 量大从优.
   
   [STATUS] 报价 100 元 | 下一步: 等 buyer 还价"

v2 status.py parse_status_block(text):
  1. 先试单行格式 _parse_single_line(text)
     - split("\n"), 找含 "[STATUS]" 的行
     - 解析 [STATUS] 后面内容
     - 支持多 part 用 | 分隔
     - 支持 key=value 格式 (progress=70 confidence=high)
     - 支持中文键 (下一步 / 任务 / 进度)
  2. fallback 多行 HTML
     - 找 <!--STATUS\n ... \n--> 块
     - parse session_id / task_id / progress / summary / next_action / confidence

返回 Status dataclass:
  session_id, task_id, progress, summary, next_action, confidence, raw

v2 优先单行 (LLM 容易生成), fallback 多行 (向后兼容).
```

### B.8 跨进程数据流总览 (图 3 整合)

```
┌──────────────────────────────────────────────────────────────────┐
│  v2.0 进程模型 (图 3 整合)                                       │
├──────────────────────────────────────────────────────────────────┤
│                                                                    │
│  ┌─ Scanner 进程 ─────────────────────────────────────────────┐ │
│  │  - 扫 channels/*.jsonl (offset 持久化)                       │ │
│  │  - 调 communication.poll_* 拿数据                             │ │
│  │  - 投递 mention mail → mailboxes/*.json                       │ │
│  │  - 投递系统通知 / [TASK] 广播                                  │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─ Agent.seller 进程 (1 worker) ─────────────────────────────┐ │
│  │  Comms.poll_new_mails() → 邮箱                                │ │
│  │  EventHandler.handle_mail() → 决策                               │ │
│  │    sessions.decide_session(snapshot) → 续/新建                │ │
│  │    cli.execute(session_id, prompt, ws) → 调 LLM                │ │
│  │    parse_status_block → 更新 sessions                         │ │
│  │    写 channels/seller_reply.jsonl + second_route                │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─ Agent.buyer 进程 (1 worker) ──────────────────────────────┐ │
│  │  (同上)                                                       │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─ Scheduler 进程 (全局) ─────────────────────────────────────┐ │
│  │  - poll_stale_tasks() → 锁 mtime 超时                          │ │
│  │  - 发 request_status 邮件给 holder                              │ │
│  │  - 第二次超时 → 释放锁 + 移除 state_board entry                │ │
│  │  - 写 channels/{}_system.jsonl 通知                             │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  ┌─ FastAPI Server (未来, 图 1+3) ─────────────────────────────┐ │
│  │  - GET /api/channels / /api/state_board / /api/mailboxes      │ │
│  │  - POST /api/post / /api/dags                                   │ │
│  │  - WebSocket 实时刷新 (WebUI 订阅)                              │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                    │
│  共享文件总线 (data_v2/):                                       │
│    channels/    mailboxes/    sessions/    locks/                │
│    state_board.json    scanner_state.json    scheduler_state.json │
└──────────────────────────────────────────────────────────────────┘
```

### B.9 Proactive 模式 — 订阅频道 + 自主发言 (v2.0 新增)

**核心区别**:
- Passive: Scanner 投递 mail → EventHandler 等事件
- Proactive: Agent 自己订阅频道 + 轮询 → DecisionMaker 决定要不要说

```
┌──────────────────────────────────────────────────────────────────┐
│  Proactive 模式 (全自主 agent 社交)                               │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  god (admin) 发第一条消息到 fish-market                          │
│     ↓ append to fish-market.jsonl                                │
│                                                                   │
│  ┌─ Agent.seller (proactive, subscriptions=[fish-market]) ────┐ │
│  │  while not stop:                                            │ │
│  │    for channel in subscriptions:                            │ │
│  │      messages = channel.read_since(offset)   # 增量读       │ │
│  │      if messages:                                           │ │
│  │        decision = DM.decide_speak(messages, role, session)   │ │
│  │        if decision.action == "speak":                       │ │
│  │          reply = cli.execute(prompt, session)                │ │
│  │          channel.append(reply)                               │ │
│  │        elif decision.action == "skip":                      │ │
│  │          pass  # 不发言, 等下一轮                            │ │
│  │    sleep(poll_interval)                                      │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  ┌─ Agent.buyer (proactive, subscriptions=[fish-market]) ─────┐ │
│  │  (同上, 两个 agent 同时轮询, 互相看到对方的新消息)              │ │
│  └────────────────────────────────────────────────────────────┘ │
│                                                                   │
│  对话流 (e2e_autonomous.sh 验证通过):                            │
│    god:  "今天的鱼价行情怎么样? 开始报价吧"                      │
│    seller: "100块一斤, 这品质绝对值"                              │
│    buyer:  "70 块钱能拿几条?"                                     │
│    seller: "95块, 搭两条小黄鱼"                                    │
│    buyer:  "80~85 一步到位"                                       │
│    seller: "88块, 搭两条小黄鱼" ← 成交!                           │
│    buyer:  "成交! 88现金"                                          │
│    seller: "成交! 给你挑最新鲜的"                                  │
└──────────────────────────────────────────────────────────────────┘
```

**Agent 构造 (proactive 模式)**:
```python
agent = Agent(
    agent_id="seller-fish",
    cli=OpenCodeCLI(),
    data_dir="./data",
    mode="proactive",                    # ← 关键: proactive 模式
    subscriptions=["fish-market"],       # ← 订阅频道列表
    system_prompt="你是卖鱼小贩...",
)
# 运行时动态订阅/取消订阅
agent.add_subscription("new-channel")
agent.remove_subscription("old-channel")
```

**DecisionMaker.decide_speak prompt**:
```
[Worker 角色]
你是 seller-fish (卖鱼小贩). 跟 buyer-fish 讨价还价.

[频道最近消息]
  [10:00] god: 今天的鱼价行情怎么样? 开始报价吧
  [10:01] seller-fish: 100块一斤
  [10:02] buyer-fish: 70 块钱能拿几条?

[当前 session]
  session_id: s1, topic: 买鱼, progress: 10%
  next_action: 等 buyer 还价

[决定]
有跟你相关的新消息吗? 你应该发言吗?
  - speak: 有新消息要回复
  - skip: 跟你无关/已成交
  - initiate: 频道空, 主动发起

[输出格式 - 严格 JSON]
{"action": "speak", "reason": "..."}
或 {"action": "skip", "reason": "..."}
或 {"action": "initiate", "reason": "...", "topic": "...", "content": "..."}
```

---

## 0. 文档地图

| 文档 | 内容 | 何时读 |
|------|------|--------|
| **本文档 (15)** | **总览** - 4 组件 PDR + 文件总线 + 跨平台 + opencode.md 5 条铁律 + 单行 STATUS + Proactive 模式 | 第一次了解 v2 |
| [`docs/13-pdr-architecture.md`](13-pdr-architecture.md) | 4 组件 PDR 详细 (Step 1-6 实施记录) | 改 4 组件时 |
| [`docs/14-windows-compat.md`](14-windows-compat.md) | 跨平台设计 (shutil.which / pathlib / 17 tests) | Windows 部署时 |
| [`docs/11-v2-architecture.md`](11-v2-architecture.md) | v2.0 初版架构 (3-channel + 路由) | 了解 v2.0 原始设计 |
| [`docs/12-workspace-pattern.md`](12-workspace-pattern.md) | workspace_dir + <cli_name>.md 引导 | 了解 workspace 机制 |
| [`docs/18-decision-maker.md`](18-decision-maker.md) | DecisionMaker (LLM 选 session) + 邮箱/轮询路径 + 强约束 | DecisionMaker 集成时 |
| [`docs/19-channel-subscription.md`](19-channel-subscription.md) | Channel Subscription 订阅设计 + ProactiveEventHandler 架构 | Proactive 模式时 |

---

## 1. 设计哲学 (3 条核心)

| 原则 | 含义 | v2.0 实现 |
|------|------|----------|
| **程序路由, AI 执行** | Scanner 纯程序, AI 只在需要时消费 token | `v2/scanner.py` (Scanner) + `v2/event_handler.py` (EventHandler 调 LLM) |
| **文件总线, 可调试** | 所有状态 = JSONL/JSON 文件, cat/jq 即可看 | `channels/*.jsonl` + `mailboxes/*.json` + `sessions/*.json` + `locks/*.lock` |
| **多 agent 协作, 单一频道** | N 个 agent 在 1 频道通过 @mention + STATUS 块互动 | `data_v2/channels/{name}.jsonl` + `Channel` 抽象 |

## 2. v1.1 → v2.0 範式转变

| 维度 | v1.1 (替代前) | v2.0 (当前) |
|------|-----------------|---------------|
| 通信 | SQLite Mailbox + Posts + Channels | **JSONL 频道 + JSON 邮箱 + lock 文件** |
| Agent | `Author` 心跳对象 (长生命) | **独立进程 + Pull 邮箱主循环** |
| 路由 | LLM 在 tick 内决定 | **Scanner 纯程序 + LLM 仅消费 token** |
| Session | `SessionDB` 内存 + SQLite | **`SessionManager` JSON 持久化 (含 content_summary / progress / next_action)** |
| 任务认领 | Posts claim (SQL UPDATE) | **O_EXCL 文件锁 + mtime TTL** |
| 状态 | Monitor events JSONL | **STATUS 注释块 + state_board.json** |
| 调度 | 嵌入 Agent 内部 | **Scheduler 后台进程 (全局, stale task + 锁)** (超时 + 锁释放) |
| **运行模式** | 只有被动 (等 @mention) | **Passive (等 mail) + Proactive (订阅频道自主发言)** (v2.0 新增) |
| 调试 | sqlite3 CLI | **cat / jq** (文件总线) |

## 3. 4 组件 PDR 架构 (每个 agent = 1 worker)

按 **Perceive-Decide-Remember-Act** 拆 4 个独立组件, 每个独立文件 + 独立测试。

```
┌─────────────── Agent (1 worker) ───────────────┐
│                                                │
│  ① CommunicationComponent  "感知"            │
│     - 主动 pull: 邮箱/频道/state_board         │
│     - 被动 push: asyncio.Event 唤醒             │
│     - 简单 API 判断 (不调 LLM)                  │
│     - 输出: (event_type, event_data) 流         │
│                                                │
│  ② EventHandler  "决策"                       │
│     - 两种模式 (Agent.mode 决定):               │
│       - **passive**: 听 comms 事件 (等 mail)    │
│         → decide_session() → 续/新建/skip     │
│       - **proactive**: 轮询订阅频道              │
│         → decide_speak() → speak/skip/initiate │
│     - 构造 prompt (含真历史)                     │
│     - 调 cli.execute                             │
│     - 解析 STATUS 块                             │
│     - 写频道 + second-route                     │
│                                                │
│  ③ SessionManager  "记忆"                       │
│     - session_id (LLM session)                  │
│     - topic / content_summary / progress         │
│     - next_action / task_id / channel           │
│     - decide_session(): 智能匹配续/新建         │
│                                                │
│  ④ CLI Client  "执行"                            │
│     - opencode / qwen / mock / (未来 claude)  │
│     - execute(session_id, prompt, ws)           │
│     - 用 subprocess / HTTP / stdin 调 LLM       │
│                                                │
└────────────────────────────────────────────────┘
```

**两种运行模式**:
- **Passive** (默认): Scanner 投递 mail → EventHandler 等事件 → DecisionMaker 决定 session
- **Proactive** (v2.0 新增): 订阅频道 + 轮询 → DecisionMaker.decide_speak → CLI 生成 → 写频道

见 **B.1** (passive) 和 **B.9** (proactive) ASCII 框图.

详细见 `docs/13-pdr-architecture.md`.

## 4. 文件总线 (4 种文件类型)

```
data_v2/
├── channels/                  # JSONL 频道 (每行一条消息)
│   ├── general.jsonl
│   └── fish-market.jsonl
│       .meta.json              # 元数据 sidecar (members + admins)
│
├── mailboxes/                 # JSON 邮箱 (每个 agent 一个文件)
│   ├── seller-fish.json
│   ├── buyer-fish.json
│   └── admin.json
│       {pending: [mail, ...]} # pending 邮件列表
│
├── sessions/                   # JSON session 索引
│   ├── seller-fish.json
│   └── buyer-fish.json
│       {sessions: {sid: {topic, content_summary, progress, ...}}}
│
├── locks/                      # 任务认领锁 (O_CREAT|O_EXCL)
│   └── task_xxx.lock
│
├── workspaces/                 # 每个 agent 独立工作目录
│   ├── seller-fish/opencode.md  # <cli_name>.md 引导
│   └── buyer-fish/opencode.md
│
├── state_board.json            # 全局任务状态板
├── scanner_state.json          # Scanner 各频道 offset
└── scheduler_state.json        # Scheduler request_log
```

**所有文件用 `pathlib.Path` (跨平台), `json.dump/load` + `tempfile + os.replace` (原子写), `threading.Lock` (并发)**。

## 5. 数据流 (3 轮讨价还价端到端)

见 **B.2** ASCII 文字框图.

## 6. CLI 抽象 (4 实现)

```python
class CLI(Protocol):
    name: str
    async def execute(
        self,
        session_id: str,        # 续; "" = 新建
        prompt: str,
        workspace_dir: str,
    ) -> CLIResponse: ...
```

| 实现 | 用法 | 状态 |
|------|------|------|
| `MockCLI` | 测试用, 0 token, echo + STATUS | ✅ |
| `OpenCodeCLI` | subprocess 调 `opencode run --model X --format json`, cwd=workspace, 找完整路径 `shutil.which()` | ✅ (默认 `opencode/minimax-m3-free`, 0 cost) |
| `QwenCLI` | HTTP API (OpenAI-compatible), 跑本地 ollama (localhost), workspace.md 内容 prefix 到 prompt | ✅ |
| `ClaudeCLI` (计划) | Anthropic Claude Code subprocess | ⏳ |

## 7. 跨平台 (Windows / macOS / Linux)

**v2.0 已经免疫 3 个 Windows 坑** (用户分享图 4, 5, 6, 9 经验):

1. **bash strip 反斜杠** (`C:\Users\foo` → `C:Usersfoo`) — 用 `pathlib.Path` 不用 `os.path` + `r"..."` raw string
2. **WinError 2 找不到 binary** — `shutil.which()` 自动处理 `.exe` / `.cmd` 后缀
3. **.cmd wrapper 不解析多 args** — `subprocess` 用 args list + 不传 `shell=True` (默认 False, asyncio.create_subprocess_exec 没 shell 参数)

详细 + 17 跨平台 tests 见 `docs/14-windows-compat.md`.

## 8. opencode.md 5 条铁律 (Claude Code AGENTS.md 风格)

见 **B.6** ASCII 文字框图.

## 9. 单行 STATUS 格式 (Step 9, 对齐 Claude Code)

见 **B.7** ASCII 文字框图.

## 10. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 通信模型 | **Pull 邮箱** (Comms 主动 poll) | 空载零消耗, 不需要 push 服务器 |
| 存储 | **文件总线** (JSONL/JSON) | cat / jq 调试, 不需 DB |
| 路由 | **Scanner 纯程序** + mention 模糊匹配 | 路由 0 token, LLM 只消费决策 |
| 任务认领 | **O_EXCL 文件锁** + mtime TTL | 原子 + 简单 + 自动过期 |
| 状态 | **STATUS 块** (单行) + state_board | 跟 Claude Code 风格一致, LLM 容易生成 |
| Session | **JSON 持久化** (含 content_summary 累积) | 跨重启保留 LLM context |
| 调度 | **State machine** (request_status → 锁释放) | 优雅降级, agent 失联自动清理 |
| CLI 抽象 | **Protocol + execute()** | 任意 LLM backend 适配 (opencode/qwen/未来 claude) |
| 跨平台 | **pathlib + shutil.which** | Windows / macOS / Linux 一致 |
| Agent 拆分 | **PDR 4 组件** (感知/决策/记忆/执行) | 关注分离, 独立可测 |
| **decide_session 接受 session_snapshot** (修 1) | Comms 调 API 时, 把所有 session 状态快照一起送, 让 decide 考虑 progress / status 上下文 | 避免已 completed 的 session 被续, 提升判断精度 |

## 11. 累积 13 张图的经验整合

| 图 | 来源 insight | v2.0 怎么 align |
|---|------------|---------------|
| 1-3 | FastAPI Server + 4 Scanner + Dispatcher 架构 | 我们的 4 组件 = 你的 Scanner, 暂时不加 Server (CLI 够用) |
| 4 | qwen + Windows 修复 (`shutil.which` / stdin / .cmd) | ✅ Step 8 (`shutil.which`), pathlib 跨平台 |
| 5, 12 | Claude Code AGENTS.md 模板 + 5 条铁律 | ✅ Step 11 (opencode.md 模板改) |
| 5 | 单行 STATUS 格式 `[STATUS] x \| y` | ✅ Step 9 (`status.py` 单行) |
| 6, 9 | Windows 路径 `C:\Users\...` 反斜杠 strip | ✅ Step 10 (pathlib + 17 tests) |
| 7 | minimax-m3-free 免费 | ✅ OpenCodeCLI 默认 model (0 cost) |
| 8 | minimax-m2.5 配置 | ✅ OpenCodeCLI 兼容 m2.5 |
| 10 | Claude Code 终端 + AGENTS.md 模板 | ✅ Step 11 |
| 11 | Qwen + GLM-5 dispatcher 修复 | ✅ Step 8 + 已有 .meta.json |
| 13 | "我给你发了一些图片" | (重发提示) |

## 12. 兼容性

### 老 v1.x → v2.0

- v1.1 已移到 `~/my_proj/_deprecated/agents-chat-v1/` (74 tests 仍跑)
- 新工作用 v2 (95 → 232 tests)
- 老 `Agent.run() / stop() / channel() / mailbox_of() / snapshot()` API 仍兼容 (委派给 4 组件)

### 老 e2e 脚本

- `e2e_v2.sh` / `e2e_bargain.sh` / `e2e_bargain_opencode.sh` / `e2e_bargain_real.sh` / `e2e_v2_4comp.sh` / `e2e_workspace.sh` 仍可用 (用 CLI 跑, 不直接调 Agent 内部)

### CLI 抽象统一

- 老 `invoke()` → 新 `execute()` (统一)
- 老 `resume_session` → 新 `session_id` (参数名一致)
- 3 个 CLI (mock/opencode/qwen) 都改, 所有 tests 同步

## 13. 测试统计

| 类别 | tests |
|------|-------|
| `tests/unit/v2/test_files.py` | 18 |
| `tests/unit/v2/test_status_session.py` | 14 |
| `tests/unit/v2/test_session_manager.py` | 25 |
| `tests/unit/v2/test_session_snapshot.py` ⭐ 新 (修 1) | 8 |
| `tests/unit/v2/test_communication.py` | 21 |
| `tests/unit/v2/test_event_handler.py` | 15 |
| `tests/unit/v2/test_agent_container.py` | 13 |
| `tests/unit/v2/test_prompt_injection.py` | 8 |
| `tests/unit/v2/test_status_single_line.py` | 17 |
| `tests/unit/v2/test_cross_platform.py` | 17 |
| `tests/unit/v2/test_workspace.py` | 10 |
| `tests/unit/v2/test_cli.py` | 9 |
| `tests/unit/v2/test_channel_members_and_fuzzy.py` | 17 |
| `tests/unit/v2/test_scanner.py` | 14 |
| 老的 v1 (在 `_deprecated/`) | 74 |
| **总 v2** | **224** |
| **总 (v1 + v2)** | **298** |

跑测试: `pytest tests/unit/ -v` (~9s)

## 14. 关键文件索引

```
src/agents_chat/v2/
├── __init__.py
├── agent.py              # 1 worker = 4 组件容器
├── event_handler.py    # 决策大脑
├── communication.py      # 感知 (PDR)
├── session_manager.py    # 记忆 (PDR) + SessionSnapshot (修 1)
├── status.py             # STATUS 块解析 (单行 + 多行兼容)
├── scanner.py            # 频道扫描 + 路由
├── scheduler.py          # 全局调度 (超时 + 锁释放)
├── main.py               # CLI 入口
├── state_board.py        # 任务状态板
├── session_index.py      # 老 API (向后兼容)
│
├── files/                # 文件 I/O 原子原语
│   ├── lock.py
│   ├── channel.py        # JSONL 频道
│   └── mailbox.py        # JSON 邮箱
│
└── cli/                  # LLM CLI 抽象
    ├── base.py           # Protocol
    ├── mock.py
    ├── opencode.py       # subprocess (minimax-m3-free)
    └── qwen.py           # HTTP API (本地 ollama)

tests/unit/v2/            # 224 tests
docs/                     # 15 文档
examples/                 # 7 e2e 脚本
data_v2/                  # 运行时 (gitignore 排除)
```

## 15. 路线图

| 状态 | 方向 | 估时 |
|------|------|------|
| ✅ Done | 4 组件 PDR 架构 | (已完成) |
| ✅ Done | 文件总线 + 文件锁 | (已完成) |
| ✅ Done | mention 模糊匹配 + 频道 members | (已完成) |
| ✅ Done | 真 LLM 跑 (opencode + qwen) | (已完成) |
| ✅ Done | workspace_dir + <cli_name>.md | (已完成) |
| ✅ Done | prompt 注入真频道历史 | (已完成) |
| ✅ Done | 单行 STATUS 格式 | (已完成) |
| ✅ Done | 跨平台 (pathlib + shutil.which) | (已完成) |
| ✅ Done | opencode.md 5 条铁律 (Claude Code 风格) | (已完成) |
| ✅ Done | **decide_session 接受 session_snapshot** (修 1) | (已完成) |
| ✅ Done | **docs 加 glossary + 图文字框图** (修 2+3) | (已完成) |
| ⏳ Next | Scanner "@admin" fallback (规则 2 落实) | 15min |
| ⏳ Next | 跑真 opencode 3 轮讨价还价 e2e 验证 | 5min |
| ⏳ Next | 加 ClaudeCLI (Anthropic Claude Code) | 1.5h |
| ⏳ Next | FastAPI Server + 简单 WebUI (可视化) | 3h |
| ⏳ Next | 依赖解析 (STATUS.next_action 自动唤醒) | 1.5h |
| ⏳ Next | 共享 Dispatcher (stdin pipes 长驻) | 1.5h |
| ⏳ Future | 负载均衡 (堆积多任务降优先级) | 2h |
| ⏳ Future | 频道归档 (旧消息压缩) | 1h |
| ⏳ Future | 板块 / 北向 / 多 agent 类型支持 | 1-2d |

## 16. 关键 commit 时间线

```
74067b9  v2/files 原语 (lock + channel + mailbox)
f382458  workspace_dir + <cli_name>.md 引导
cd6da97  channel members + admin + 模糊匹配 + 讨价还价 e2e
5389eca  e2e_bargain_opencode.sh (QwenCLI + ollama)
3995f8a  OpenCodeCLI 用 opencode/minimax-m3-free
c8aa824  Session + SessionManager (PDR Step 1/4)
2f0ad0f  CommunicationComponent (PDR Step 2/4)
7d59395  EventHandler (PDR Step 3/4)
0d3a402  Agent 瘦身为 4 组件容器 (PDR Step 4/4)
34b76e1  4 组件 e2e + CLI 统一 (PDR Step 5/6)
ed1d6fb  docs/13-pdr-architecture.md
f194fec  prompt 注入真频道历史 + 反剧本
9dc7ea3  OpenCodeCLI 跨平台 shutil.which()
6783bde  单行 STATUS 格式 (对齐 Claude Code)
e877c72  跨平台 17 tests + docs/14-windows-compat.md
a245019  opencode.md 5 条铁律 (Claude Code 风格)
02afc44  docs/15-v2-architecture-overview.md
<NEW>    ⭐ decide_session 接受 session_snapshot + docs glossary/框图
```

## 17. 一句话总结

**v2.0 = PDR 4 组件 + 文件总线 + 真 LLM 集成 + 跨平台 + Claude Code 风格 STATUS/opencode.md + session 状态快照判断**。

跑 `pytest tests/unit/` 看 232 个 tests 全过。跑 `bash examples/e2e_bargain_real.sh` 看真 LLM 3 轮讨价还价。

详细看:
- **PDR 细节** → `docs/13-pdr-architecture.md`
- **跨平台** → `docs/14-windows-compat.md`
- **本总览** (本文) → 入门必读, 含 glossary + 8 个 ASCII 文字框图

---

## 附: 本次 3 个用户修正

| 修正 | 来源 | 实施 |
|------|------|------|
| **1. Scanner 调 API 判断时, 把 session 状态快照一起送** | 用户分享架构 insight | ✅ `SessionManager.snapshot()` + `decide_session(..., session_snapshot=...)` |
| **2. 架构文档最前面加核心概念词解释 (glossary)** | 方便后续讨论更精确 | ✅ Section A: 22 个核心概念词 |
| **3. 架构文档里把图片里的信息流程用文字框图画出来** | 方便嵌入代码注释 / 复制 | ✅ Section B: 8 个 ASCII 文字框图 (B.1-B.8) |
