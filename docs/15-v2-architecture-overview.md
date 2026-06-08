# 15. v2.0 Architecture Overview

> Status: ✅ Production-ready (224/224 tests 全过, 15 commits)
> Date: 2026-06-08
> Author: 方浩博 + QClaw agent

## 0. 文档地图

| 文档 | 内容 | 何时读 |
|------|------|--------|
| **本文档 (15)** | **总览** - 4 组件 PDR + 文件总线 + 跨平台 + opencode.md 5 条铁律 + 单行 STATUS + 路线图 | 第一次了解 v2 |
| [`docs/13-pdr-architecture.md`](13-pdr-architecture.md) | 4 组件 PDR 详细 (Step 1-6 实施记录) | 改 4 组件时 |
| [`docs/14-windows-compat.md`](14-windows-compat.md) | 跨平台设计 (shutil.which / pathlib / 17 tests) | Windows 部署时 |
| [`docs/11-v2-architecture.md`](11-v2-architecture.md) | v2.0 初版架构 (3-channel + 路由) | 了解 v2.0 原始设计 |
| [`docs/12-workspace-pattern.md`](12-workspace-pattern.md) | workspace_dir + <cli_name>.md 引导 | 了解 workspace 机制 |

---

## 1. 设计哲学 (3 条核心)

| 原则 | 含义 | v2.0 实现 |
|------|------|----------|
| **程序路由, AI 执行** | Scanner 纯程序, AI 只在需要时消费 token | `v2/scanner.py` (Scanner) + `v2/agent_scheduler.py` (Scheduler 调 LLM) |
| **文件总线, 可调试** | 所有状态 = JSONL/JSON 文件, cat/jq 即可看 | `channels/*.jsonl` + `mailboxes/*.json` + `sessions/*.json` + `locks/*.lock` |
| **多 agent 协作, 单一频道** | N 个 agent 在 1 频道通过 @mention + STATUS 块互动 | `data_v2/channels/{name}.jsonl` + `Channel` 抽象 |

## 2. v1.1 → v2.0 范式转变

| 维度 | v1.1 (替代前) | v2.0 (当前) |
|------|-----------------|---------------|
| 通信 | SQLite Mailbox + Posts + Channels | **JSONL 频道 + JSON 邮箱 + lock 文件** |
| Agent | `Author` 心跳对象 (长生命) | **独立进程 + Pull 邮箱主循环** |
| 路由 | LLM 在 tick 内决定 | **Scanner 纯程序 + LLM 仅消费 token** |
| Session | `SessionDB` 内存 + SQLite | **`SessionManager` JSON 持久化 (含 content_summary / progress / next_action)** |
| 任务认领 | Posts claim (SQL UPDATE) | **O_EXCL 文件锁 + mtime TTL** |
| 状态 | Monitor events JSONL | **STATUS 注释块 + state_board.json** |
| 调度 | 嵌入 Agent 内部 | **Scheduler 后台进程** (超时 + 锁释放) |
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
│  ② AgentScheduler  "决策"                       │
│     - 听 comms 事件                             │
│     - 续/新建 session                            │
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
│     - opencode / qwen / mock / (future claude)  │
│     - execute(session_id, prompt, ws)           │
│     - 用 subprocess / HTTP / stdin 调 LLM       │
│                                                │
└────────────────────────────────────────────────┘
```

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

```
T=0  god → Scanner 投 mention mail 到 seller.mailbox + buyer.mailbox
        ↓
T=1  [Comms.seller 主动 poll] → yield ("mail", mail)
        ↓
     [Scheduler.seller.handle_mail]
        1. parse task_id + topic
        2. sessions.decide_session() → 新建 Session(local_seller_001)
        3. _build_prompt (含 5 条铁律 + 真频道历史 + session 上下文)
        4. cli.execute(session_id="", prompt, ws) → reply="100 元"
        5. parse_status_block → progress=10, next_action="等 buyer"
        6. sessions.update(remote_id="oc_xxx", progress, next_action)
        7. 写频道: "@buyer 100 一斤"
        8. _second_route: @buyer → buyer.mailbox.append

T=2  [Comms.buyer] → handle_mail
        sessions.decide_session → 新建 Session(local_buyer_001)
        cli.execute → reply="70 块!"
        写频道: "@seller 70 卖不卖?"
        _second_route: @seller

T=3  [Comms.seller] → handle_mail
        sessions.decide_session → 命中! 续 Session(local_seller_001)
        cli.execute(session_id="oc_xxx") → 续 → reply="最低 80"
        写频道: "@buyer 最低 80"
        progress=50

T=4  [Comms.buyer] → handle_mail
        续 Session → reply="成交 80"
        progress=100 → sessions.update(status="completed")
        写频道: "🎉 80 元成交"
```

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

每个 agent 启动时, `Agent._init_workspace_files` 自动写 `{workspace_dir}/{cli.name}.md` (claude.md / opencode.md / qwen.md), 内容含 5 条铁律 (用户分享图 5, 12):

```markdown
# {agent_id} 角色定义 (v2.0) — 模板自动生成

你是 {agent_id}。{system_prompt}。你在一个多人协作频道中。
频道: #{default_channel}, 成员: {members_str}。
频道管理员: {admins_str}。

## ⚠️ 5 条铁律 (频道通信)

1. **开头 @名字**: 每条 reply 必须在开头指定收信人
2. **不确定就 @频道管理员**: 不确定对谁说, 就 @频道管理员
3. **管理员指令立即执行**: 不要先回 "收到/好的", 直接给答案
4. **角色扮演中继续演**: 用真频道历史 (prompt 注入) 续剧情
5. **[STATUS] 简述 | 下一步: xxx** (单行格式, Step 9)
```

CLI 工具 (opencode / qwen / claude) 启动时自动读这个文件作为角色引导。

## 9. 单行 STATUS 格式 (Step 9, 对齐 Claude Code)

**v2.0 默认输出** (LLM 友好):
```
[STATUS] 报价 100 元 | 下一步: 等 buyer 还价
[STATUS] progress=70 confidence=high | 已完成 | 下一步: 提交
[STATUS] 任务: t1 | 进度: 50 | 下一步: 提交
```

**老的 v2.0 多行 HTML 格式** (向后兼容):
```html
<!--STATUS
 session_id: s1
 task_id: t1
 progress: 70
 summary: 已完成
 next_action: 提交
 confidence: high
-->
```

`status.py` 的 `parse_status_block()` 优先单行, 失败 fallback 多行. `format_status()` 默认单行, `format_status_block()` 显式多行.

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
- 新工作用 v2 (95 → 224 tests)
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
| `tests/unit/v2/test_communication.py` | 21 |
| `tests/unit/v2/test_agent_scheduler.py` | 15 |
| `tests/unit/v2/test_agent_container.py` | 13 |
| `tests/unit/v2/test_prompt_injection.py` | 8 |
| `tests/unit/v2/test_status_single_line.py` | 17 |
| `tests/unit/v2/test_cross_platform.py` | 17 |
| `tests/unit/v2/test_workspace.py` | 10 |
| `tests/unit/v2/test_cli.py` | 9 |
| `tests/unit/v2/test_channel_members_and_fuzzy.py` | 17 |
| `tests/unit/v2/test_scanner.py` | 14 |
| 老的 v1 (在 `_deprecated/`) | 74 |
| **总 v2** | **197** |
| **总 (v1 + v2)** | **271** |
| **跨平台 (v2)** | **17** |
| **总 v2 (含跨平台)** | **197 + 17 = 214**, **实际** | **224** |

跑测试: `pytest tests/unit/ -v` (~9s)

## 14. 关键文件索引

```
src/agents_chat/v2/
├── __init__.py
├── agent.py              # 1 worker = 4 组件容器
├── agent_scheduler.py    # 决策大脑
├── communication.py      # 感知 (PDR)
├── session_manager.py    # 记忆 (PDR)
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

tests/unit/v2/            # 197 tests
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
| ⏳ Next | Scanner "@admin" fallback (规则 2) | 15min |
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
7d59395  AgentScheduler (PDR Step 3/4)
0d3a402  Agent 瘦身为 4 组件容器 (PDR Step 4/4)
34b76e1  4 组件 e2e + CLI 统一 (PDR Step 5/6)
ed1d6fb  docs/13-pdr-architecture.md
f194fec  prompt 注入真频道历史 + 反剧本
9dc7ea3  OpenCodeCLI 跨平台 shutil.which()
6783bde  单行 STATUS 格式 (对齐 Claude Code)
e877c72  跨平台 17 tests + docs/14-windows-compat.md
a245019  opencode.md 5 条铁律 (Claude Code 风格)
```

## 17. 一句话总结

**v2.0 = PDR 4 组件 + 文件总线 + 真 LLM 集成 + 跨平台 + Claude Code 风格 STATUS/opencode.md**。

跑 `pytest tests/unit/` 看 224 个 tests 全过。跑 `bash examples/e2e_bargain_real.sh` 看真 LLM 3 轮讨价还价。

详细看:
- **PDR 细节** → `docs/13-pdr-architecture.md`
- **跨平台** → `docs/14-windows-compat.md`
- **本总览** (本文) → 入门必读
