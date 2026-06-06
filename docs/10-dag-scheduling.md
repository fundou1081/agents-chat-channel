# 10. DAG 并行调度 (Phase 3)

> Status: 设计中 → 实现中
> Author: 方浩博
> Date: 2026-06-06

## 目标

让 god 可以一次性给一个**多步任务**（带依赖关系），自动并行调度多个 author 干，不再走 PM 串行派活。

**对比**：

| 场景 | 旧方案 (PM) | 新方案 (Orchestrator + DAG) |
|------|-------------|------------------------------|
| 单步任务 ("写个 hello.py") | ✅ PM 串行派给 zhang | ✅ PM (仍可用) |
| 多步并行 ("后端 + 前端") | ❌ PM 一次派一个，串行 | ✅ Orchestrator 并行派发 |
| 多步带依赖 ("API→集成") | ❌ PM 不知道依赖 | ✅ DAG 显式声明 deps |
| 失败处理 | ❌ PM 收到 NAK 不知如何继续 | ✅ Orchestrator 标 `blocked` 并汇报 god |
| 可视化 | ❌ PM 心跳状态不直观 | ✅ Gantt / DAG 节点图 (Phase 4) |

## 核心抽象

### DAG (有向无环图)

```
        ┌── N1: 后端 API ──┐
god ──▶│                  ├──▶ N3: 集成 ──▶ god 汇报
        └── N2: 前端 UI ───┘
```

| 概念 | 含义 | 持久化 |
|------|------|--------|
| `DAG` | 一个完整任务图 | `dags` 表 |
| `DagNode` | 图上的一个节点 | `dag_nodes` 表 |
| `depends_on` | 节点间依赖 | `dag_nodes.depends_on` (JSON list) |
| Status | DAG 状态机 | `dags.status` |

### 状态机

**DAG 状态**:
```
pending → active → (completed | failed | cancelled)
                  ↗
       (god submit)
```

**Node 状态**:
```
pending → running → (done | failed)
                ↓
              blocked (上游有 failed)
```

## 数据流

```
┌─────┐  submit DAG   ┌──────────────┐  dispatch (Mail)  ┌──────┐
│ god │ ─────────────▶│ Orchestrator │ ──────────────────▶│ zhang│
│     │               │   (Author)   │                    │  /li │
└─────┘               └──────┬───────┘                    └──┬───┘
       ▲                     │                               │
       │ report              │ scan active DAGs              │ work
       │                     │ + check deps                  │
       │                     ▼                               │
       │              ┌──────────────┐                       │
       └──────────────│  DagStore    │◀──── reply mail ──────┘
                      │  (SQLite)    │
                      └──────────────┘
```

**通信通道**（与 3-Channel 架构保持一致）:

| 步骤 | 通道 | 形式 |
|------|------|------|
| god 提交 DAG | **Posts** | `Post(kind="dag_dispatch", body=JSON, attached_dag_id=...)` |
| Orchestrator 派发 Node | **Mailbox DM** | `Mail(recipients=[assignee], tags=["dag_dispatch", "DAG_ID=x", "NODE_ID=y"], ...)` |
| Author 完工回报 | **Mailbox DM** | `Mail(sender=author, to="orchestrator", in_reply_to=mail_id, body="DONE" or "FAILED: ...")` |
| Orchestrator 状态更新 | **DagStore** | `DagStore.update_node_status(...)` |
| Orchestrator 完成通知 god | **Mailbox DM** | `Mail(sender="orchestrator", to="god", subject="DAG completed: ...")` |

## Orchestrator Author 设计

**为什么 Orchestrator 是 Author？**
- 跟其他 author 一样有 heartbeat (调度要主动)
- 跟其他 author 一样有 `mailbox` (收回报邮件)
- 跟其他 author 一样注册到 `HeartbeatRegistry`
- **唯一不同**：`OrchestratorAuthor._tick` **不调 LLM**，纯确定性逻辑

**Persona**:
```yaml
id: orchestrator
display_name: 调度员
emoji: 🧭
title: DAG 调度器
heartbeat_seconds: 5  # 调度频率高, 实时推进
llm_backend: none      # 关键: 不调 LLM
workdir: /tmp/orchestrator
```

**Tick 逻辑**:
```python
async def _tick(self):
    # 1. 处理回报邮件
    reports = await self.mailbox.fetch_unread(owner="orchestrator", limit=20)
    for mail in reports:
        await self._handle_report(mail)
        await self.mailbox.mark_read([mail.id])

    # 2. 扫 active DAGs
    active = await self.dag_store.list_active_dags()
    for dag in active:
        ready = self._find_ready_nodes(dag)
        for node in ready:
            await self._dispatch_node(dag, node)

    # 3. 检查 DAG 整体完成 / 失败 → 通知 god
    for dag in active:
        if all_done(dag):
            await self._complete_dag(dag)
        elif any_blocked_with_no_recovery(dag):
            await self._fail_dag(dag)
```

**调度算法 (`_find_ready_nodes`)**:
```python
def _find_ready_nodes(self, dag: DAG) -> list[DagNode]:
    """返回所有 status=pending 且 deps 都 done 的节点."""
    done_ids = {n.id for n in dag.nodes if n.status == "done"}
    ready = []
    for n in dag.nodes:
        if n.status != "pending":
            continue
        if all(dep in done_ids for dep in n.depends_on):
            ready.append(n)
    return ready
```

## 失败处理

| 场景 | 行为 |
|------|------|
| 单 node FAIL | 标 `failed` → 所有依赖它的下游标 `blocked` → DAG 状态 `failed` → 通知 god |
| Orchestrator 自身挂 | 跟普通 author 一样，heartbeat 重启；DAG 状态在 SQLite 不丢 |
| Author 一直不回执 | Orchestrator 跑 N 个 tick 没收到回执 → 标 `failed` + timeout 错误 |
| god 想取消 DAG | 走 API: `POST /api/dags/{id}/cancel` → 标 `cancelled` → 不再 dispatch |

## API

```
POST   /api/dags/submit            # god 提交 DAG
GET    /api/dags                   # 列所有 DAGs
GET    /api/dags/{id}              # 看某个 DAG (含 nodes 状态)
POST   /api/dags/{id}/cancel       # 取消
GET    /api/dags/{id}/gantt        # Gantt 数据 (Phase 4)
```

**Submit 例子**:
```json
POST /api/dags/submit
{
  "title": "做 Web 应用",
  "description": "前后端 + 集成",
  "created_by": "god",
  "nodes": [
    {"id": "api", "title": "后端 API", "body": "REST /api/users", "assignee": "li-backend", "depends_on": []},
    {"id": "ui",  "title": "前端 UI", "body": "React 首页",       "assignee": "zhang-frontend", "depends_on": []},
    {"id": "integ", "title": "集成",  "body": "联调",            "assignee": "zhang-frontend", "depends_on": ["api", "ui"]}
  ]
}
```

**Response**:
```json
{
  "dag_id": "dag-abc123",
  "post_id": "post-xyz789",
  "status": "active",
  "nodes": [
    {"id": "api", "status": "running", "started_at": "..."},
    {"id": "ui",  "status": "running", "started_at": "..."},
    {"id": "integ", "status": "pending"}
  ]
}
```

## 跟现有架构关系

| 现有组件 | 改动 | 说明 |
|---------|------|------|
| `models.Post` | 加 `kind="dag_dispatch"` | 完全兼容，4 → 5 种 |
| `models.Mail` | 加 `tags: list[str]` | 让 author 看到 DAG_ID tag 自动回执 |
| `Author` | 加 `tags` 字段读取 | LLM prompt 注入 "如果看到 `DAG_ID` tag，回执到 `orchestrator`" |
| `PostsDB` | 加 `list_active_dag_dispatches()` | 给 Orchestrator 用 |
| `Mailbox` | **不改** | 直接用 |
| `Channels` | **不改** | 跟 DAG 无关 |
| `PM persona` | **不改** | 仍管单步任务 |

## 关键决策点（已选）

1. **DAG 谁描述？** → god 写 JSON (方案 a，**简单可调试**)
2. **DAG 走 Posts 还是 Mail？** → Posts (`dag_dispatch` kind，**audit 友好**)
3. **Node 派发走 Posts 还是 Mail？** → Mail (1-to-1 精确派活，**跟 Mailbox 语义契合**)
4. **Author 怎么知道回执？** → Mail tags (`DAG_ID=x NODE_ID=y`，**LLM 看到就回**)
5. **Orchestrator 调 LLM 吗？** → 不调，**纯确定性调度** (调度逻辑可预测、可测试)
6. **失败处理** → Node 失败 → 下游 blocked → DAG failed → 通知 god (简单明确)

## 文件清单

```
src/agents_chat/
├── models.py                   # M  DAG, DagNode, DagStatus, DagNodeStatus
├── storage/
│   └── dag_db.py               # NEW  DagStore (dags + dag_nodes 两表)
├── author/
│   └── orchestrator.py         # NEW  OrchestratorAuthor (无 LLM tick)
└── web/
    └── server.py               # M  /api/dags/* endpoints

tests/unit/
└── test_dag.py                 # NEW  ~15 tests (store + scheduling + dispatch)

docs/
└── 10-dag-scheduling.md        # NEW  本文档

examples/
└── e2e_dag.sh                  # NEW  3-node DAG e2e
```

## 测试策略

| 层级 | 覆盖 |
|------|------|
| Unit | DagStore CRUD / deps 解析 / 状态机转移 / 调度算法 |
| Integration | Orchestrator tick: submit → 派发 → 模拟回执 → 推进 |
| E2E | 3-node DAG (api/ui parallel → integ) 真实 LLM 跑通 |

## 未决问题

- [ ] **超时**: node 多久没回执算 timeout？(建议 1 小时，PM heartbeat 120 倍)
- [ ] **优先级**: DAG 节点间有没有 priority？目前 FIFO
- [ ] **重试**: failed 节点能不能 god 触发 retry？(API: `/api/dags/{id}/retry_node`)
- [ ] **持久化 vs 内存**: DAG 状态目前全在 SQLite，重启不丢 ✅

## 进度

- [x] Phase 3.0: 设计文档
- [ ] Phase 3.1: models 加 DAG
- [ ] Phase 3.2: DagStore
- [ ] Phase 3.3: OrchestratorAuthor
- [ ] Phase 3.4: API + main.py
- [ ] Phase 3.5: e2e_dag.sh
