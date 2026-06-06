# 11. v2.0 Multi-Agent Mailbox Communication Architecture (实施文档)

> Status: ✅ Implemented (M1 + 调度, 95 tests + E2E 验证)
> 替代 v1.1 (3-channel Author 抽象)
> Author: 方浩博
> Date: 2026-06-06

## 0. v1.1 → v2.0 范式转变总结

| 维度 | v1.1 (替代前) | v2.0 (替代后) |
|------|----------------|----------------|
| 通信 | Mailbox (SQLite) + Posts (SQLite) + Channels (SQLite) | **JSONL 频道 + JSON 邮箱 + lock 文件** |
| Agent 模型 | 长生命 Author 对象 + heartbeat_loop | **独立 Agent 进程 + pull 邮箱主循环** |
| 路由 | LLM 在 tick 内决定 | **Scanner 纯程序** |
| Session | SessionDB 内存 + SQLite 持久化 | **本地 JSON 映射 → CLI resume** |
| 任务认领 | 走 Posts claim (UPDATE 原子) | **O_EXCL 文件锁** |
| 状态报告 | Monitor event JSONL | **STATUS 注释块** + state_board |
| 调度 | OrchestratorAuthor (Phase 3) | **Scheduler 后台进程** |
| 调试 | sqlite3 CLI | **cat / jq** |

## 1. 核心抽象

### 1.1 文件总线 (v2/files/)

| 类 | 路径 | 原子性 |
|----|------|--------|
| `Mailbox` | `mailboxes/{agent_id}.json` | 原子写 (tmp + `os.replace`), 内部 `threading.Lock` |
| `Channel` | `channels/{name}.jsonl` | 追加模式 (`open('a')` 原子, < PIPE_BUF) |
| `lock` (函数集) | `locks/task_{id}.lock` | `O_CREAT | O_EXCL` 原子创建, mtime 判 TTL |

### 1.2 Agent (`v2/agent.py`)

每个 Agent 绑一个 CLI 程序 (qwen / opencode / mock), 独立进程, 拉邮箱主循环:

```
while not stop:
    mails = mailbox.read_and_clear()
    if mails: process_batch(mails)
    sleep(poll_interval)
```

处理一封 mail (`_process_one`):
1. 提取 task_id (从 mail 显式 / content `task_xxx` / ref_msg_id / hash)
2. type 路由: `request_status` → 写 STATUS 块; `system_notify` → ack; 其他 → claim
3. acquire 锁 (mention 强制, task_broadcast/opportunity 抢)
4. `state_board.claim()` + `session_index` 匹配/新建 local_sess
5. 构造 prompt (含 system + mail content + STATUS 块提示)
6. 调 CLI (`resume=remote_sess` 如有)
7. 写频道 reply + 提取 STATUS 块
8. `state_board.update_from_status()`
9. `lock_refresh()` 续约
10. 二次路由: reply 里的 @mention → 投递 mention 邮件

### 1.3 Scanner (`v2/scanner.py`)

纯程序路由, **不调 LLM**:

```
while not stop:
    for ch in channels:
        msgs, new_off = Channel(ch).read_since(offset)
        for msg in msgs: route(msg)
        offset = new_off
    save_state()
    sleep(scan_interval)
```

路由规则:
- `msg.mentions` 非空 → 投递 `mention` 邮件到目标 agent
- `[TASK task_xxx]` 标记 → 广播 `task_broadcast` 给所有 agent
- 任何消息含 STATUS 块 → `state_board.update_from_status()`
- agent 自己写的消息不投递给自己 (避免循环)

### 1.4 Scheduler (`v2/scheduler.py`)

全局调度中心, 定期检查超时任务:

```
while not stop:
    stale = state_board.list_stale(stale_ttl)
    for task in stale: handle_stale(task)
    sleep(check_interval)
```

Stale 处理状态机 (per task):
1. **第一次**发现 stale → 发 `request_status` 邮件 + 写频道
2. **第二次** (grace_period 后仍 stale) → 强制释放锁 + 移除 state_board + 写频道通知可重新认领

### 1.5 状态报告块 (STATUS)

每条 agent reply **必须**嵌入:
```
<!--STATUS
 session_id: local_sess_001
 task_id: task_042
 progress: 70
 summary: 已定位连接池耗尽, 正分析连接泄漏点
 next_action: 审计最近上线的服务代码, 预计30分钟
 confidence: high
-->
```

- Scanner 解析 → 更新 state_board
- progress 0-100, 100 = 任务完成
- 缺 STATUS 块 → Scheduler 不会 timeout (heartbeat 没更新)

### 1.6 CLI 抽象 (`v2/cli/`)

- `MockCLI` — 测试用, 0 token, echo + STATUS
- `OpenCodeCLI` — subprocess 调 opencode CLI (`opencode run "prompt" --session <id>`)
- `QwenCLI` — HTTP API + 本地 history 模拟 resume (qwen 无原生 --resume)

## 2. 目录结构

```
src/agents_chat/
├── v1/                         # ⚠️  deprecated (替代前代码, 不再维护)
│   ├── author/                 # (原 author/, 整体 git mv)
│   ├── llm/
│   ├── storage/
│   ├── web/
│   ├── models.py
│   ├── heartbeat.py
│   ├── monitor.py
│   ├── policy.py
│   └── main.py
└── v2/                         # ✅ 新版本
    ├── __init__.py
    ├── files/                  # 文件 I/O 原子原语
    │   ├── lock.py
    │   ├── channel.py
    │   └── mailbox.py
    ├── cli/                    # CLI 程序适配
    │   ├── base.py             #   Protocol
    │   ├── mock.py             #   MockCLI
    │   ├── opencode.py         #   OpenCodeCLI (subprocess)
    │   └── qwen.py             #   QwenCLI (HTTP + history)
    ├── status.py               # STATUS 块解析
    ├── session_index.py        # local → remote 映射
    ├── state_board.py          # 全局任务状态板
    ├── agent.py                # Agent 主循环
    ├── scanner.py              # 纯程序路由
    ├── scheduler.py            # 超时检测
    └── main.py                 # CLI 入口
```

`data_v2/` 目录结构:
```
data_v2/
├── README.md
├── channels/
│   └── general.jsonl           # JSONL 频道
├── mailboxes/
│   ├── qwencode.json           # 每个 agent 一个 JSON
│   └── claude.json
├── sessions/
│   ├── qwencode.json           # local → remote 映射
│   └── claude.json
├── locks/
│   └── task_xxx.lock           # 任务认领锁 (5min TTL)
├── state_board.json            # 全局任务状态板
├── scanner_state.json          # 各频道 offset
├── scheduler_state.json        # request_log
└── qwen_history/               # qwen HTTP API history (QwenCLI 用)
    ├── qwen_abc.json
    └── ...
```

## 3. CLI 使用

```bash
# 初始化
python -m agents_chat.v2.main init --data-dir ./data_v2

# 启动一组 (开发模式, 一起跑 scanner + scheduler + 2 agent)
python -m agents_chat.v2.main run-all \
    --data-dir ./data_v2 \
    --agents qwencode claude \
    --cli mock

# 生产模式: 分别跑 (不同终端)
python -m agents_chat.v2.main run-scanner --data-dir ./data_v2
python -m agents_chat.v2.main run-scheduler --data-dir ./data_v2 --stale-ttl 300
python -m agents_chat.v2.main run-agent qwencode --cli qwen --data-dir ./data_v2
python -m agents_chat.v2.main run-agent claude --cli opencode --data-dir ./data_v2

# 交互 helper
python -m agents_chat.v2.main post general "@qwencode 帮我修 bug" --sender god
python -m agents_chat.v2.main tail general --n 20
python -m agents_chat.v2.main status                    # 所有 task
python -m agents_chat.v2.main status task_042           # 单个 task
python -m agents_chat.v2.main inbox qwencode            # 看邮箱 pending
python -m agents_chat.v2.main reset --yes              # ⚠️ 清空
```

## 4. 任务状态机

```
   ┌──── submit ────┐
   ▼                │
 [active]            │
   │
   ├── all node done ───▶ [completed] ──▶ 释放锁, 通知 god
   │
   ├── any node failed ──▶ [failed] ──▶ 强制释放, 通知 god
   │
   └── heartbeat 超时 ──▶ scheduler 检测
                            │
                            ├── 1st: 发 request_status
                            └── 2nd: 强制释放锁 + 移除 state_board
```

## 5. 端到端示例 (3 agent: god / qwencode / claude)

```bash
# 1. 启动 run-all
python -m agents_chat.v2.main run-all --data-dir ./data_v2 \
    --agents qwencode claude --cli mock &

# 2. god 发 [TASK] 广播
python -m agents_chat.v2.main post general \
    "[TASK task_demo_001] 写一个 hello.py" --sender god

# 3. god @ 单一 agent
python -m agents_chat.v2.main post general \
    "@qwencode 帮我看下 task_demo_001" --sender god

# 4. 看结果
python -m agents_chat.v2.main tail general --n 20
# 看到: god (task_broadcast) → qwencode + claude 各 reply
#       god (mention) → qwencode reply (用同 session resume)
```

## 6. 测试

| 层级 | 数量 | 覆盖 |
|------|------|------|
| Unit (v2/files) | 18 | lock acquire/release/expire/refresh + channel append/read_since/tail + mailbox append/peek/atomic |
| Unit (v2/status + session) | 17 | STATUS 块解析 (basic/missing/last/clamp/cn/format) + SessionIndex CRUD/persistence |
| Unit (v2/cli) | 9 | MockCLI first/resume/extract_task_id |
| Unit (v2/state_board) | 11 | claim/update/preserve_meta/create_unknown/stale/release/complete |
| Integration (v2/agent) | 14 | process_mention/no_claim_skip/request_status/second_route/session_resume/run_loop/CLI_error |
| Integration (v2/scanner) | 14 | mention/task_broadcast/status/offset/skip_own/run_loop |
| Integration (v2/scheduler) | 5 | no_stale/first_request/second_release/missing_agent/run_loop |
| Smoke (v2/main) | 7 | init/post/status/tail/inbox via subprocess |
| **v2 总计** | **95** | ✅ |
| v1.1 保留 | 74 | 回归基线 (不维护) |
| **总计** | **169** | ✅ |

跑测试: `pytest tests/unit/ -v` (5s)
跑 E2E: `bash examples/e2e_v2.sh` (15s)

## 7. 关键设计决策 (跟 v2.0 设计文档对齐)

| 设计点 | 选择 | 理由 |
|--------|------|------|
| 通信模型 | Pull 邮箱 | 空载零消耗, 程序路由 |
| 存储 | 文件总线 (JSONL/JSON) | 可 cat / jq 调试, 简化 |
| 路由 | 纯程序 Scanner | 不消耗 token, 确定性 |
| 任务认领 | O_EXCL 文件锁 + mtime TTL | 原子, 简单, 自动过期 |
| Session | 本地映射 + CLI resume | 单 program 多会话 |
| 状态可观测 | STATUS 块 + state_board | 全局可见, 程序提取 |
| 失败处理 | Scheduler 自动检测 + 锁释放 | 优雅降级 |
| CLI 适配 | Protocol + 3 实现 | mock / opencode / qwen 通用 |

## 8. 未实现 (后续 Phase)

- [ ] **依赖解析**: `next_action: "等待 task_xxx"` → 自动唤醒相关 agent
- [ ] **负载均衡**: 某 agent 堆积多任务 → 降低向它分发优先级
- [ ] **Web 面板**: 实时展示 state_board / 频道 / 邮箱
- [ ] **真实 CLI 集成测试**: opencode / qwen 真实环境跑通
- [ ] **多 Scanner 分片**: 不同 Scanner 负责不同频道
- [ ] **频道归档**: 旧消息压缩存档

## 9. 进度

- [x] **M1 核心闭环**:
  - [x] v2/files (lock + channel + mailbox)
  - [x] v2/status (STATUS 解析)
  - [x] v2/session_index
  - [x] v2/cli (base + mock + opencode + qwen)
  - [x] v2/state_board
  - [x] v2/agent
  - [x] v2/scanner
- [x] **M1 + 调度**:
  - [x] v2/scheduler (超时 + 锁释放)
  - [x] v2/main (CLI 入口)
  - [x] e2e_v2.sh
  - [x] docs (本文档)
- [ ] **Phase B**: git mv `src/agents_chat/*` → `v1/` 目录
- [ ] **M2**: 真实 CLI 集成 (opencode + qwen)
- [ ] **M3**: 依赖解析 + 负载均衡
- [ ] **M4**: Web 面板
