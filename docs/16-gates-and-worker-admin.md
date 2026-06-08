# 16. Worker Gates & Admin Roles

> Status: ✅ v2.0 新增 (300/300 tests 全过)
> Date: 2026-06-08
> 配套 commit: 即将 (大改动单独 commit)

---

## 0. TL;DR

两个新能力:
1. **Worker Gate** — 输入/输出过滤 (sanitize / reject), 可插拔
2. **Worker 当 Admin** — 频道管理员可以是 worker agent (不是只有人类)

---

## 1. Worker Gate 是什么

每个 worker (Agent) 可以在两个位置加 gate:
- **Input gate** — 收到 mail 后, 构造 prompt 前跑 (过滤/改写用户输入)
- **Output gate** — LLM 生成 reply 后, 写频道前跑 (过滤/改写 LLM 输出)

Gate 三种行为:
- **allow** — 通过 (可改写 text, 例如截断 / sanitize)
- **deny** — 拒绝 (整个 mail 丢弃, 写 system 消息到频道, 不调 LLM)
- **skip** — gate 不实现该方向, 跳过 (用于只在 input 或 output 跑的 gate)

---

## 2. Gate API

```python
from agents_chat.v2.gates import (
    Gate, GateResult, GateChain,
    MaxLengthGate, SecretLeakGate, ControlCharsGate,
)

# 一个 gate 是个类, 实现 check_input / check_output
class MyGate:
    name = "my_gate"
    def check_input(self, text: str) -> GateResult: ...
    def check_output(self, text: str) -> GateResult: ...

# GateResult
result = GateResult.allow("text", gate="my_gate")        # 通过
result = GateResult.deny("text", "reason", "my_gate")    # 拒绝
result.allowed    # bool
result.text       # 改写后的 text
result.reason     # 拒绝原因 / 改写原因
result.gate       # 哪个 gate 做的判定

# GateChain: 顺序应用, 任一 deny 短路
chain = GateChain([gate1, gate2, gate3], direction="input")
result = chain.run("text")
```

---

## 3. 3 个 Builtin Gates

### 3.1 MaxLengthGate

截断过长内容, 加 `[truncated]` 后缀。

```python
# Input 默认 4000, Output 默认 8000
g = MaxLengthGate(max_chars=4000, suffix="...[truncated]")
result = g.check_input(long_text)
# result.allowed = True (截断是 sanitize, 不算拒绝)
# result.text = long_text[:4000] + "...[truncated]"
```

### 3.2 SecretLeakGate

检测 API key / 密码 / 私钥, 默认 sanitize (`[REDACTED:type]`), strict 模式 deny。

```python
# 默认 sanitize
g = SecretLeakGate()
r = g.check_input("my key is sk-abcdefghijklmnopqrstuvwxyz")
# r.text = "my key is [REDACTED:openai-style key]"

# Strict 模式拒绝
g = SecretLeakGate(strict=True)
r = g.check_input("my key is sk-...")
# r.allowed = False, r.reason = "detected secrets: openai-style key"
```

**检测模式**:
- OpenAI / Anthropic / OpenRouter API keys (`sk-...`, `sk-ant-...`, `sk-or-v1-...`)
- AWS access key (`AKIA...`)
- GitHub tokens (`ghp_...`, `gho_...`, `ghu_...`, `ghs_...`, `ghr_...`)
- Bearer tokens
- Password=`...` / api_key=`...`
- `-----BEGIN ... PRIVATE KEY-----`

### 3.3 ControlCharsGate

去除控制字符 (NUL / SOH / 其它不可打印), 保留 `\n \r \t`。

```python
# 保留空白
g = ControlCharsGate(keep_whitespace=True)
r = g.check_input("line1\nline2\ttab\x00null")
# r.text = "line1\nline2\ttabnull"
```

---

## 4. 在 EventHandler 启用 Gate

`Agent.__init__` 加两个参数:
```python
from agents_chat.v2.gates import MaxLengthGate, SecretLeakGate, ControlCharsGate

agent = Agent(
    agent_id="gated",
    cli=OpenCodeCLI(),
    data_dir="./data_v2",
    input_gates=[
        MaxLengthGate(max_chars=4000),       # 截断过长输入
        SecretLeakGate(),                     # sanitize secret
    ],
    output_gates=[
        SecretLeakGate(),                     # LLM 输出也 sanitize
        MaxLengthGate(max_chars=8000),       # 截断过长输出
        ControlCharsGate(keep_whitespace=True),  # 去控制字符
    ],
)
```

默认 `input_gates=None, output_gates=None` — **完全向后兼容**, 旧代码不传 = 不过滤。

---

## 5. Gate 拒绝时的行为

Input/output gate 拒绝时:
1. **不调 LLM** (input 拒绝) 或 **不写频道** (output 拒绝)
2. 写一条 `type=system` 消息到频道, 含拒绝原因
3. STATUS 块标记: `progress=0, summary=gate 拒绝`, `next_action=等待人工调整`
4. 不更新 session (没产生有效 output)

例子 (频道消息):
```json
{
  "from": "gated",
  "type": "system",
  "content": "[gated] input gate REJECTED: [max_length_4000] truncated 8000 → 4000 chars\n\n<!--STATUS\n progress: 0\n summary: input gate 拒绝 (truncated 8000 → 4000 chars)\n next_action: 等待人工调整\n-->"
}
```

---

## 6. 测试覆盖 (`tests/unit/v2/test_gates.py`)

25+ tests:
- `TestGateResult`: 2 (allow/deny 构造)
- `TestMaxLengthGate`: 5 (短/长/边界/input/output/name)
- `TestSecretLeakGate`: 10 (openai / anthropic / aws / github / bearer / password / private key / 无 secret / strict / output)
- `TestControlCharsGate`: 5 (保留空白 / 去 NUL / 去 SOH / 不保留空白 / output)
- `TestGateChain`: 7 (空 / 单个 / 顺序 pipeline / 短路 deny / direction / len+bool / 跳过缺失 direction)
- `TestEventHandlerGateIntegration`: 3 (input deny / output sanitize / 无 gate 默认行为)

---

## 7. Worker 当 Admin

### 7.1 设计

频道元数据 `.meta.json` 加两个字段:
- `admins` (worker admins) — 有 mailbox, scanner 会投递
- `human_admins` (人类 admins) — 没 mailbox, scanner 不投递 (他们直接看频道)

**为什么分开**: 老逻辑把 admins 列表当字符串, scanner 投递时 `_deliver_mail` 检查 `mb.path.exists()` 自然跳过没 mailbox 的目标 — 但 fallback 选 admin 时**没**区分,可能选到人类 admin(投递失败)。

新逻辑:
- `Channel.add_admin(agent_id, is_worker=True)` — 默认 (兼容老调用)
- `Channel.add_admin(agent_id, is_worker=False)` — 写 `human_admins`
- `Channel.list_admins()` — 只返回 worker admins (兼容老 API)
- `Channel.list_human_admins()` — 返回人类 admins (新)
- `Channel.is_admin(agent_id, is_worker=True|False|None)` — 精确判断
- `Channel.remove_admin(agent_id, is_worker=True)` — 移除

### 7.2 Scanner Fallback (新优先级)

`_resolve_admin_fallback(target, admins, exclude)` 改造:

| 优先级 | 规则 | 备注 |
|--------|------|------|
| 1 | target 含 admin 关键字 (`admin` / `god` / `频道` / `频道管理员` / `manager`) → 投那个 worker admin | **必须是 worker admin** (在 `known_agents` 里) |
| 2 | admins 里有任何 worker admin → 投第一个 | "worker 优先" |
| 3 | 没 worker admin (都是人类) → 返回 None | 不投递 |

**关键变化**: 老逻辑里"admin 名字含 admin 关键字就命中"在新逻辑下要求**它必须是 worker** (有 mailbox), 人类 admin 即使名字含 admin 也不投递 (避免 _deliver_mail 失败)。

### 7.3 用法

```python
from agents_chat.v2.files.channel import Channel

ch = Channel("data/channels/general.jsonl", "general")
ch.add_member("user_alice")           # 人类用户
ch.add_member("worker_bot")           # worker
ch.add_admin("worker_bot")            # worker 当 admin (默认)
ch.add_admin("user_ou_abc", is_worker=False)  # 人类 admin (看频道, 不收 mail)

# 查询
ch.list_admins()         # ["worker_bot"]
ch.list_human_admins()   # ["user_ou_abc"]
ch.is_admin("worker_bot", is_worker=True)   # True
ch.is_admin("user_ou_abc", is_worker=True)  # False
ch.is_admin("user_ou_abc", is_worker=False) # True
ch.is_admin("worker_bot")                    # True (任意类型)
ch.is_admin("nobody")                        # False

# 移除
ch.remove_admin("worker_bot", is_worker=True)
```

### 7.4 频道里的效果

- 用户发 `@admin 求救` (mention=`["admin"]`) → scanner fallback → 投 `worker_bot` (worker admin)
- `user_ou_abc` (人类 admin) 看频道, 看到消息 (jsonl 文件直接读)
- 人类 admin 不会因为 `@admin` 收到 mailbox 邮件

### 7.5 老 metadata 兼容

老的 `.meta.json` (没 `human_admins` / `admin_types` 字段) 加载时:
- 自动补 `human_admins=[]`, `admin_types={}`
- 老 `admins` 列表里的内容**当作 worker admin** (跟以前行为一致)
- 损坏的 JSON 文件 fallback 到默认值, 不抛异常

### 7.6 测试覆盖 (`tests/unit/v2/test_worker_admin.py`)

18+ tests:
- `TestChannelAdminBasic`: 8 (add_admin 默认/显式/human/重复/自动 member/human 不 member/混合)
- `TestChannelAdminQueries`: 6 (is_admin worker/human/none + remove + 错 type)
- `TestChannelMetadataCompat`: 2 (老格式加载 / 损坏 JSON)
- `TestScannerAdminFallbackWorkerPriority`: 7 (worker 优先 / 只 human 返回 None / admin keyword / god keyword / exclude self / exclude only human / name 关键字)
- `TestEndToEndWorkerAdmin`: 3 (worker admin 收 mention / 人类 admin 不收 / 混合 admin 选 worker)

---

## 8. 兼容性表

| 老调用 | 新行为 |
|--------|--------|
| `Channel.add_admin(id)` | 同 `add_admin(id, is_worker=True)`, 写到 `admins` |
| `Channel.list_admins()` | 只返回 worker admins (跟以前行为一致, 因为以前没人类 admin) |
| `Channel.is_admin(id)` | 等同 `is_admin(id, is_worker=None)`, 任意类型都算 |
| `Scanner._resolve_admin_fallback(target, admins)` | 老行为: 选第一个非自己的 admin; 新行为: 优先选 worker admin, 人类 admin 不投递 |
| `Agent(agent_id, cli, ...)` | 完全没变 (没传 gates = 不过滤) |
| `Agent(agent_id, cli, ..., input_gates=[])` | 启用但空 = 跟不传一样 |
| `Scanner(data_dir=...)` | 完全没变 |

**没有 breaking change**:
- 没 `human_admins` 的老 metadata 加载后 `list_human_admins()` 返回 `[]`
- 老 `admins` 列表里的内容仍按 worker admin 处理
- 老 scanner 测试只有 1 个回归 (`test_admin_name_matches_keyword`), 已修: 给 `admin_bot` 加 mailbox

---

## 9. 测试统计

| 阶段 | tests |
|------|-------|
| 改动前 (前 1 轮) | 232 |
| + 修 1 (SessionSnapshot) | 240 |
| + 本次 (Gates + Worker Admin) | 300 |
| 跑时 | ~9s |

---

## 10. 关键 commit 时间线 (本批)

```
1. feat(gates): 新增 gates.py 模块 (Gate 协议 + GateChain + 3 个 builtin)
2. feat(event_handler): EventHandler 集成 input/output gates
3. feat(agent): Agent 容器透传 gates 参数
4. feat(channel): Channel.add_admin(agent_id, is_worker) 兼容接口 + human_admins 分离
5. feat(scanner): _resolve_admin_fallback 改为只投 worker admin
6. test(gates): 25+ tests 覆盖
7. test(admin): 18+ tests 覆盖 worker admin
8. fix(scanner): 老 test_admin_name_matches_keyword 加 mailbox 兼容
9. docs: 16-gates-and-worker-admin.md
```
