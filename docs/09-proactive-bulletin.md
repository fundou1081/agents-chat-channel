# 09 — Proactive Bulletin Board (双架构)

## 解决的问题

之前的 email model 只能做**被动通信**:
- god 派活给 PM → PM 派活给 zhang → zhang 干活
- 每个 author 只能看到**自己 inbox 里的**邮件
- 如果 god 扔一个"团队,做个新首页,谁想做" → 邮件给 god,没人接 (routing 失败 drop)

**真实工作场景需要主动沟通**:
- god 扔一个"团队,谁想做"任务到**任务板** (无指定收件人)
- 工程师**主动扫**任务板,看到匹配的 (frontend + UI) 就**认领**
- god 再扔"@zhang 看一下这个 API 命名" — zhang 心跳扫到 mention 自己的,**主动回**

## 双架构

| 架构 | 数据流 | 触发 | 适用 |
|---|---|---|---|
| **被动 (Email)** | god → PM → zhang | 收件人精确匹配 | 明确派活链 |
| **主动 (Bulletin)** | god → 中央板 → 任何匹配 author 扫 | 角色/mention 匹配 | 无主任务, 自由认领 |

## 核心数据模型

```python
@dataclass
class Announcement:
    id: str
    kind: str  # "broadcast" | "unassigned_task" | "discussion"
    title: str
    body: str
    posted_by: str
    posted_at: str
    tags: list[str] = []
    required_role: str = ""      # "frontend" | "backend" | "any"
    claimed_by: str = ""         # 谁先 claim 谁
    status: str = "open"         # "open" | "claimed" | "closed" | "expired"
    expires_at: str = ""
    thread_id: str = ""
```

## BulletinDB API

```python
class BulletinDB:
    async def post(ann)                  # 发公告
    async def list_open(kind, limit)     # 列开放
    async def list_for_author(p, limit)  # 跟 persona 相关的 (核心!)
    async def claim(ann_id, claimer)     # 认领 (原子, 防 race)
    async def close(ann_id)              # 关
    async def expire_old()               # 自动 expire
```

## Relevance 逻辑 (list_for_author)

```python
def _is_relevant(ann, persona) -> bool:
    if ann.kind == "broadcast":
        return True                          # 所有人都看
    if ann.kind == "unassigned_task":
        if ann.required_role in ("", "any"):
            return True                      # 不限角色
        if ann.required_role in persona.title.lower():
            return True                      # 角色匹配
        if ann.required_role in persona.id.lower():
            return True
        return False
    if ann.kind == "discussion":
        return _mentions(ann, persona)       # 提了我才看
```

`_mentions` 检查 `ann.title + ann.body + ann.required_role + ann.tags` 是否含
`persona.id / display_name / title` 任一。

## Author 集成

`_tick()` 里加:

```python
# 4a. 扫中央任务板 (主动通信)
bulletins_for_me = []
if self.bulletin:
    bulletins_for_me = await self.bulletin.list_for_author(
        self.persona, limit=10
    )

ctx = TickContext(
    persona=self.persona,
    new_mail=new_mail,
    active_sessions=list(self.sessions.values()),
    bulletins=bulletins_for_me,  # ← NEW
    ...
)
```

`_execute()` 里加:

```python
elif action.type == "claim_announcement":
    ann_id = action.payload.get("id", "")
    if self.bulletin and ann_id:
        success, msg = await self.bulletin.claim(ann_id, self.persona.id)
        if success:
            # 认领成功 → monitor 记录 + total_actions++
            ...
```

## Prompt 集成 (think.py)

LLM 在 TickContext 看到 `bulletins` 字段, prompt 教它:

```markdown
# 任务板 (Central Bulletin, 2 条跟你相关)
📌 [unassigned_task] [role: frontend] 新首页 UI  (id=abc123)
   做个新首页,有想法的接
   tags: 前端, ui
📌 [broadcast] 团队周会  (id=def456)
   今天 3pm,所有人

# 你的判断
- 如果任务板里有 unassigned_task 且 required_role 匹配你, 可以 claim:
  actions 里加 {"type": "claim_announcement", "payload": {"id": "<ann_id>"}}
- 如果任务板里有 broadcast 跟你有关, 可以回邮件参与
- 如果任务板里有 discussion mention 你, 必须回邮件
```

## API

| Method | Path | 作用 |
|---|---|---|
| GET | `/api/bulletin?kind=...&status=open` | 列公告 |
| POST | `/api/bulletin/post` | 发公告 |
| POST | `/api/bulletin/{id}/claim` | 认领 |
| POST | `/api/bulletin/{id}/close` | 关闭 |

发公告自动 burst 所有 author (让他们立即 tick 看到)。

## Web UI

`📋 任务板` tab:
- 表单: kind (select), title, role, tags, body
- 列表: 开放公告, 每个有 ✋ Claim / ❌ Close 按钮
- 已认领: 灰色按钮 + 标注 claimed_by

## 端到端流程

```
god:  POST /api/bulletin/post
      {kind: "unassigned_task", title: "新首页 UI",
       required_role: "frontend", tags: ["前端", "ui"]}
              ↓
      BulletinDB.post()  → 存到 bulletins.db
      registry.trigger_burst_all()  → 3 个 author burst tick
              ↓
PM 心跳 → 扫 bulletin → "新首页 UI" 不匹配 PM → skip
zhang 心跳 → 扫 bulletin → role=frontend 匹配 "前端工程师" → 看到
li 心跳 → 扫 bulletin → role=frontend 不匹配 "后端工程师" → skip
              ↓
zhang 看到 → 决策: claim
actions: [{"type": "claim_announcement", "payload": {"id": "abc123"}}]
              ↓
_execute: bulletin.claim(abc123, "zhang-frontend")
              ↓
  原子操作: UPDATE SET status=claimed, claimed_by=zhang WHERE id=abc AND status=open
              ↓
li 再次心跳 → 扫 bulletin → abc123 已是 claimed → 不相关 → skip
              ↓
zhang 后续: opencode 改文件, 发邮件回 god
```

## 文件

- `src/agents_chat/models.py` — `Announcement` 模型
- `src/agents_chat/storage/bulletin_db.py` — SQLite bulletin
- `src/agents_chat/author/base.py` — `_tick` + `_execute` 集成
- `src/agents_chat/author/think.py` — prompt 加 bulletins
- `src/agents_chat/main.py` — `get_bulletin_db()` factory
- `src/agents_chat/web/server.py` — `/api/bulletin/*` endpoints
- `src/agents_chat/web/ui/index.html` — 📋 任务板 tab
- `tests/unit/test_bulletin.py` — 14 个测试

## 测试

```
70/70 unit tests pass (含 14 个新 bulletin 测试)
```

## 实测

```bash
$ python -m agents_chat.main web --llm auto
# 浏览器开 http://localhost:7331
# 1. 切到 "📋 任务板" tab
# 2. 发布一个 "unassigned_task", required_role=frontend
# 3. 看 zhang-frontend 卡片: status 变 "thinking" (因为 bullet 触发 tick)
# 4. zhang 决策, 可能 claim (opencode 跑 write tool)
# 5. 看 "📋 任务板" tab: 该项变灰, 标 "已认领 by zhang-frontend"
```

## 双架构总结

```
                  被动 (Email)               主动 (Bulletin)
                  ─────────────               ─────────────────
  数据            个人 mailbox              中央 bulletin board
  查询            fetch_unread(me)          list_for_author(me)
  触发            recipient 精确匹配        role / mention 模糊匹配
  收件            显式指定                  隐式相关
  Author 行为     收 + 回                   扫 + claim
  适用            明确派活链                无主任务 / 讨论
  谁负责          god / PM                  author 自己
```

**两套机制并存, 覆盖完整工作场景**。
