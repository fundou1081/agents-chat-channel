# 17. v2.0 HTTP Server (FastAPI)

> Status: ✅ v2.0 新增 (340/340 tests 全过)
> Date: 2026-06-08
> 配套 commit: 即将 (新模块单独 commit)

---

## 0. TL;DR

`agents-chat-channel` 之前是 **CLI-only** (v2 main.py 提供 init/run-agent/run-scanner 等子命令)。
现在加了 **FastAPI HTTP server**, 提供 REST API + WebUI 静态文件 mount 占位。

启动:
```bash
python -m agents_chat.v2.server --port 8765 --data-dir ./data_v2
# → http://127.0.0.1:8765/docs  (OpenAPI 自动文档)
```

---

## 1. 架构

```
+================================================================+
|                    HTTP SERVER (FastAPI)                       |
|                                                                |
|  create_app(data_dir) → app                                    |
|                                                                |
|  lifespan:                                                     |
|    startup:   pm.cleanup_finished()                            |
|    shutdown:  pm.stop_all()                                    |
|                                                                |
|  Middleware:                                                   |
|    CORSMiddleware:  allow_origins=["*"] (WebUI 准备)           |
|                                                                |
|  Static:                                                       |
|    /webui/*  ← webui/ 目录 (有就 mount, 没有就跳过)             |
+================================================================+
                              |
                              | subprocess
                              v
+================================================================+
|                  ProcessManager (in-process)                   |
|                                                                |
|  start_agent(agent_id, cli) → subprocess.Popen                 |
|  start_scanner()            → subprocess.Popen                 |
|  start_scheduler()          → subprocess.Popen                 |
|  stop(process_id)            → SIGTERM → SIGKILL                |
|                                                                |
|  状态: data_dir/processes.json (重启用)                         |
|  日志: data_dir/logs/<kind>_<id>_<ts>.log                      |
+================================================================+
                              |
                              | spawn
                              v
+================================================================+
|                  Worker / Scanner / Scheduler                  |
|                                                                |
|  python -m agents_chat.v2.main run-agent <id> --cli mock       |
|  python -m agents_chat.v2.main run-scanner                     |
|  python -m agents_chat.v2.main run-scheduler                   |
+================================================================+
```

**关键设计**:
- **ProcessManager 是 in-process** — 跟 server 同生命周期, lifespan 退出时停所有进程
- **subprocess 独立进程** — 跟 v2.0 file bus 设计一致 (multi-process via JSONL)
- **PYTHONPATH 自动设置** — 子进程能找到 `agents_chat.v2.main` 模块

---

## 2. 端点列表 (34 个)

### 2.1 Health / Root

| Method | Path | 说明 |
|--------|------|------|
| GET | `/` | 服务信息 |
| GET | `/api/health` | 健康检查 |
| GET | `/docs` | OpenAPI 文档 (FastAPI 自动) |

### 2.2 Agents (8 个)

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/agents` | 列出所有 agent (从 mailboxes/) |
| GET | `/api/agents/{id}` | agent 详情 (mailbox + sessions + process) |
| POST | `/api/agents/{id}/start` | 启动 agent 进程 (cli/capabilities/channel/system_prompt/workspace_dir/poll_interval) |
| POST | `/api/agents/{id}/stop` | 停止 agent 进程 |
| POST | `/api/agents/{id}/tick` | 触发 agent 立即处理 (发 system_notify mail) |
| GET | `/api/agents/{id}/log?tail=N` | 读 agent 日志最后 N 行 |

### 2.3 Channels (6 个)

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/channels` | 列出所有频道 (含 members + admins + human_admins) |
| GET | `/api/channels/{name}/messages?limit=N` | 频道消息 (tail) |
| POST | `/api/channels/{name}/messages` | 发消息 (from/content/type/mentions/ref_msg_id/task_id) |
| GET | `/api/channels/{name}/meta` | 频道元数据 (members + admins + human_admins) |
| POST | `/api/channels/{name}/members` | 加成员 |
| POST | `/api/channels/{name}/admins` | 加 admin (is_worker=True/False) |

### 2.4 Mailboxes (2 个)

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/mailboxes/{id}` | agent 邮箱 pending 邮件 |
| DELETE | `/api/mailboxes/{id}` | 清空邮箱 (atomic read_and_clear) |

### 2.5 Sessions (3 个)

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/sessions/{id}` | agent 全部 sessions |
| GET | `/api/sessions/{id}/active` | agent active sessions |
| POST | `/api/sessions/{id}/decide` | decide_session LLM-free API (测试用) |

### 2.6 State Board (2 个)

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/state_board` | 全局任务状态板 |
| GET | `/api/state_board/{task_id}` | 单个 task 状态 |

### 2.7 Scanner / Scheduler / Processes / Stats

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/scanner/status` | scanner offset 状态 |
| POST | `/api/scanner/start` | 启动 scanner |
| POST | `/api/scanner/stop` | 停止 scanner |
| POST | `/api/scheduler/start` | 启动 scheduler |
| POST | `/api/scheduler/stop` | 停止 scheduler |
| GET | `/api/processes` | 列出所有 managed 进程 |
| GET | `/api/processes/{id}` | 进程详情 |
| POST | `/api/processes/{id}/stop` | 停止进程 |
| GET | `/api/stats` | 简单统计 (channels/agents/messages/mails/sessions/running) |

---

## 3. 用法示例

### 3.1 启动 server

```bash
# 默认 127.0.0.1:8765 + ./data_v2
python -m agents_chat.v2.server

# 自定义端口 + data_dir
python -m agents_chat.v2.server --port 9000 --data-dir ./my_data

# dev mode (auto-reload)
python -m agents_chat.v2.server --reload
```

### 3.2 启动 agent

```bash
# 用 curl
curl -X POST http://127.0.0.1:8765/api/agents/buyer-fish/start \
  -H "Content-Type: application/json" \
  -d '{"cli": "opencode", "channel": "fish-market", "capabilities": ["bargain"]}'

# 用 httpx (Python)
import httpx
r = httpx.post("http://127.0.0.1:8765/api/agents/buyer-fish/start", json={
    "cli": "opencode",
    "channel": "fish-market",
})
print(r.json())
# {"ok": true, "process": {"process_id": "...", "pid": 12345, ...}}
```

### 3.3 发消息到频道

```bash
curl -X POST http://127.0.0.1:8765/api/channels/fish-market/messages \
  -H "Content-Type: application/json" \
  -d '{
    "from": "user_ou_abc",
    "content": "@buyer-fish 鱼怎么卖?",
    "mentions": ["buyer-fish"]
  }'
```

### 3.4 看 stats

```bash
curl http://127.0.0.1:8765/api/stats
# {
#   "channels": 3, "agents": 2,
#   "total_messages": 42, "total_mails": 5, "total_sessions": 4,
#   "running": {"agents": 2, "scanner": true, "scheduler": true}
# }
```

### 3.5 看 agent 日志

```bash
curl "http://127.0.0.1:8765/api/agents/buyer-fish/log?tail=50"
```

---

## 4. ProcessManager 详解

### 4.1 ManagedProcess 数据结构

```python
@dataclass
class ManagedProcess:
    process_id: str      # uuid4 short (12 chars)
    kind: str            # "agent" | "scanner" | "scheduler"
    agent_id: str = ""   # only for kind=agent
    cli: str = ""        # only for kind=agent
    pid: int             # subprocess.Popen.pid
    cmd: list[str]       # 完整启动命令
    log_path: str        # stdout/stderr 重定向文件
    started_at: str      # ISO 时间
    stopped_at: str      # 停止时填
    exit_code: int       # -1 = 还在跑
```

### 4.2 启动命令

**Agent**:
```bash
python -m agents_chat.v2.main run-agent <agent_id> \
  --cli <mock|qwen|opencode> \
  --data-dir <DATA_DIR> \
  --channel <CHANNEL> \
  --poll-interval <SEC> \
  [--capabilities <c1> <c2> ...] \
  [--system-prompt <TEXT>] \
  [--workspace-dir <DIR>]
```

**Scanner**:
```bash
python -m agents_chat.v2.main run-scanner \
  --data-dir <DATA_DIR> \
  --scan-interval <SEC>
```

**Scheduler**:
```bash
python -m agents_chat.v2.main run-scheduler \
  --data-dir <DATA_DIR>
```

### 4.3 状态持久化

`data_dir/processes.json`:
```json
{
  "processes": {
    "45bab8f4666e": {
      "process_id": "45bab8f4666e",
      "kind": "agent",
      "agent_id": "buyer-fish",
      "cli": "opencode",
      "pid": 12345,
      "cmd": ["python", "-m", "agents_chat.v2.main", "run-agent", "..."],
      "log_path": "/data_v2/logs/agent_buyer-fish_1780900000.log",
      "started_at": "2026-06-08T06:32:31+00:00",
      "stopped_at": "",
      "exit_code": -1
    }
  },
  "updated_at": "2026-06-08T06:32:31+00:00"
}
```

### 4.4 停止流程

```
stop(process_id):
  1. send SIGTERM → 等 5s
  2. 还活着 → SIGKILL → 强杀
  3. 更新 stopped_at + exit_code
  4. 持久化到 processes.json
```

---

## 5. 集成到 v2.0 架构

```
                    +---------------------+
                    |  WebUI (future)     |
                    |  React + WebSocket  |
                    +----------+----------+
                               | HTTP/WS
                               v
                    +---------------------+
                    |  FastAPI Server     |
                    |  (this PR)          |
                    +----+----------+-----+
                         |          |
                  ProcessManager   File Bus (data_v2/)
                         |          |
                         v          v
                  +------+-----+----+-----+
                  |   Workers    |  File    |
                  |   subprocess |  IO      |
                  +--------------+----------+
```

**WebUI 接入点** (之后做):
- `GET /api/agents` → 列出 worker 卡片
- `POST /api/agents/{id}/start` → 启动按钮
- `GET /api/channels/{name}/messages` → 频道消息流
- `POST /api/channels/{name}/messages` → 发消息
- `WS /ws` (TODO) → 实时事件流

---

## 6. 兼容性

| 调用 | 影响 |
|------|------|
| `python -m agents_chat.v2.main run-agent <id>` | **没变** — server 用同一个命令 |
| `data_v2/processes.json` | 新增, 不影响其他数据 |
| `data_v2/logs/` | 新增, `.gitignore` 已加 |
| 老 `examples/e2e_*.sh` 脚本 | **没变** — 还是直接调 main.py |

**没有 breaking change**: server 是新入口, 老 CLI 入口完全保留。

---

## 7. 测试覆盖 (`tests/unit/v2/test_server.py`)

40 tests:
- `TestHealth`: 2 (root / health)
- `TestStats`: 2 (empty / with data)
- `TestAgents`: 9 (list / get / start / duplicate / stop / tick + 4 错误路径)
- `TestChannels`: 10 (list / messages / post / meta / add_member / add_admin x2)
- `TestMailboxes`: 3 (not found / get / clear)
- `TestSessions`: 4 (empty / list / active / decide 续+新建)
- `TestStateBoard`: 3 (empty / not found / with data)
- `TestScanner`: 2 (empty / with data)
- `TestProcesses`: 3 (empty / not found / stop not found)
- `TestEndToEnd`: 2 (post message + mailbox / start→stop workflow)

**总测试数**: 340/340 passed in 10.59s (从 300 → 340, +40)

---

## 8. 关键 commit (本批)

```
1. feat(server): 新增 process_manager.py (subprocess 生命周期)
2. feat(server): 新增 server.py (FastAPI app + 34 端点)
3. fix(state_board): server 用 list_all() 不是 all()
4. fix(process_manager): PYTHONPATH = project_root (4 级父目录)
5. test(server): 40 tests 覆盖
6. docs: 17-server-api.md
```

---

## 9. 路线图 (server 之后)

- [ ] **WebUI** (React + Vite): 聊天界面 + agent 卡片 + 实时事件
- [ ] **WebSocket** `/ws`: server push 事件流 (新 message / session 变化)
- [ ] **Auth**: 简单的 token / cookie (生产用)
- [ ] **TLS / HTTPS**: 部署用
- [ ] **多 data_dir 支持**: 1 个 server 管多个项目
- [ ] **Metrics**: Prometheus / OpenTelemetry export
