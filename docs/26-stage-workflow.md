# 26. Stage-Isolated Workflow (DAG 编排 + 文件交付)

> **v2.1 设计** — 跟现有 channel "会议室" 模式**叠加**而非替换
> **状态**: 调研完成, 待实施
> **作者**: agents-chat-channel team

---

## 0. TL;DR (执行摘要)

**用户需求**: 保留现有 worker 自由通信模式, **新增 DAG 编排层**, 让特定工作流用 YAML 定义 stage 依赖 + 文件交付。

**核心范式**:
- **Stage 内**: worker 仍是 PDR 持续协作 (跟现有一样, 在私有 channel 里)
- **Stage 间**: 通过 **deliverable 文件** 显式交付 (类似 GitHub Actions artifacts)
- **调度**: YAML 定义的 DAG, scheduler 按拓扑序跑, stage 完等文件, 转交下一 stage

**跟现有架构关系**:
- ✅ **100% 兼容**, 0 破坏现有 370 tests
- ✅ 复用所有现有组件 (PDR / Channel / CLI / EventBus / busd / watchdog)
- ✅ 现有 channel 模式 (god 当导演) 继续 work
- 🆕 **新增模块** `src/agents_chat/workflow/`

**类比**:
- 现有 channel 模式 = **Slack 群** (持续, 自由)
- 新增 workflow 模式 = **GitHub Actions** (DAG, 一次性, 文件交付)

**工作量**: 1 周 (~600-800 行 + 8-10 tests + 1 文档), 0 风险 (纯新增)

**推荐**: 4 个决策都选 A (最简, 最直观, 跟现有对齐)

---

## 1. 背景与现状

### 1.1 现有架构回顾

```text
现有 agents-chat-channel v2.0.x:
├─ Worker 持续 listen channel (PDR 4 组件)
├─ Channel 自由模式 (god 当导演, 任何 worker @mention 任何 worker)
├─ CLI adapter (opencode/qwen/A2A/mock)
├─ EventBus + busd + watchdog (3 层事件驱动)
├─ WebUI 6 视图
└─ 370 tests passed
```

**核心范式**: "**会议室**" — N 个 worker 共享 N 个 channel, 自由 PDR 协调, **无界** (持续运行)。

### 1.2 用户痛点

**问题**: 现有模式适合"持续协作", 但**不适合"一次性的多阶段工作流"**:

```text
场景: "调研 → 写报告 → 审核" (一次性任务)
现有做法: 
  - 手动开 channel, 手动邀请 worker, 手动等结果, 手动转交
  - 全部依赖 god 当导演, 不可重复
  - 没有清晰的 stage 边界, 没有可审计的 deliverable

期望: 
  - 一份 YAML 描述整个工作流
  - 平台按依赖顺序自动调度 stage
  - 同 stage worker 限定范围 (避免误沟通)
  - stage 间通过**文件**显式交付 (可审计)
  - 跑完即结束 (有界)
```

### 1.3 为什么是**叠加**而非替换

**关键设计决策**: 现有 channel 模式**不删**, 跟 workflow 模式**并存**:

| 场景 | 用哪种模式 |
|------|----------|
| 持续协调 (如讨价还价 agent 持续谈判) | 现有 channel |
| god 当导演的多 team 协作 | 现有 channel |
| **一次性多阶段工作流** (如"调研 → 写报告") | **新 workflow** |
| **可重复的 pipeline** (如 CI/CD + 报告生成) | **新 workflow** |
| **stage 隔离的需求** (不同 stage 不应互见) | **新 workflow** |

**核心创新**: **"Stage 内的多 worker 仍是 PDR 自由协作, stage 间用文件做硬边界"**

---

## 2. 核心设计原则

### 2.1 5 大原则

1. **DAG 优先, agent 第二** — 用户先定义 stage 依赖 (YAML), 平台调度 worker
2. **Stage 隔离** — stage A 的 worker 看不到 stage B (通信范围严格)
3. **文件交付** — stage 间通过 deliverable 文件 (typed, 路径明确)
4. **可观测** — 每个 stage 状态/输入/输出/时长可查
5. **零破坏** — 现有 370 tests 继续过, 现有 channel 模式不变

### 2.2 跟"纯 DAG 调度器"区别

| 维度 | Airflow / Prefect | 我们 |
|------|-------------------|------|
| Stage 内部 | DAG 节点 = 1 个任务, 单进程跑 | **DAG 节点 = N 个 worker, 多进程 PDR 协作** |
| 通信 | 任务间不通信 (纯函数式) | **Stage 内 worker 仍可 PDR 自由协调** |
| 状态 | Task state (5 状态) | **Stage state + Deliverable 文件** |
| 持久化 | MetaDB (Postgres) | **文件总线 (跟现有对齐)** |
| 协调 | DAG scheduler 调度 | **DAG scheduler + stage 内 god (复用)** |
| 风格 | 工业 ETL | **LLM 协作 pipeline** |

**核心差异**: 我们把 **PDR 协作** 嵌入 DAG 节点, 而不是"DAG 节点 = 1 个函数"。

---

## 3. 核心抽象

### 3.1 四层抽象

```text
┌─────────────────────────────────────────────────────────┐
│  Workflow (DAG)                                            │
│  - YAML 定义, Pydantic 验证                               │
│  - 拓扑排序 (stage 依赖)                                   │
│  - 全局状态 (running / success / failed / partial)         │
├─────────────────────────────────────────────────────────┤
│  Stage (DAG 节点)                                          │
│  - workers 列表 (1+ 个)                                    │
│  - deliverable 契约 (路径 + schema)                       │
│  - depends_on 列表                                          │
│  - 私有 channel (stage 内 worker 共享)                     │
│  - 状态: pending → running → success/failed/timeout       │
├─────────────────────────────────────────────────────────┤
│  Worker (stage 内)                                         │
│  - 跟现有完全一样 (PDR + CLI)                              │
│  - 启动时: 加载 deliverable input (从上游 stage)            │
│  - 跑完时: 写 deliverable 文件 (给下游 stage)              │
├─────────────────────────────────────────────────────────┤
│  Deliverable (文件契约)                                    │
│  - 路径 (data/findings.json)                              │
│  - 格式 (json / markdown / text)                           │
│  - Schema (Pydantic / JSON Schema, 验证 stage 完成)         │
└─────────────────────────────────────────────────────────┘
```

### 3.2 Workflow 生命周期

```text
                    ┌─ pending (YAML 解析完, 拓扑排好)
                    │
[User submit] ──►  ├─ running  (scheduler 启 stage)
                    │     ↓
                    │   ┌─ stage 1: research  (2 workers PDR 协作)
                    │   ├─ stage 2: write     (等 stage 1 完)
                    │   └─ stage 3: review   (等 stage 2 完)
                    │
                    ├─ success (全部 stage done, deliverable 齐)
                    │
                    ├─ failed  (任一 stage failed, 可重跑)
                    │
                    └─ canceled (User 主动 cancel)
```

---

## 4. YAML Schema 详细设计

### 4.1 完整示例 (`pipeline.yaml`) — **v2 schema (checks 合并 + path/paths/dir)**

```yaml
name: research-pipeline
description: 调研 → 写报告 → 审核
version: "1.0"

# ============= DAG 拓扑 (stages 列表) =============
stages:
  - id: research
    description: "并行调研"
    timeout: 600
    workers:
      - id: researcher-1
        cli: opencode
        model: opencode/deepseek-v4-flash-free
        system_prompt: |
          你是 researcher-1. 负责搜索权威资料.
          关注 {input.topic} 的核心论点和数据来源.
      - id: researcher-2
        cli: opencode
        model: opencode/deepseek-v4-flash-free
        system_prompt: |
          你是 researcher-2. 负责交叉验证.
          找 {input.topic} 的反方观点, 反驳 researcher-1 的结论.
    # v2 deliverable: checks 合并 (declarative + scheduler 轻校验)
    deliverable:
      path: data/findings.md          # 单文件
      format: markdown               # hint (不强制)
      checks:                        # 统一检查列表 (worker 提示 + scheduler 校验)
        - "至少 3 个权威来源"        # 启发式: 纯文字 → hint
        - "## 结论"                  # 启发式: 含 ## → contains
        - "## 来源"                  # scheduler 校验
        - "中文, 2000+ 字"           # hint
      min_size: 2000                 # scheduler 轻校验 (粗粒度)

  - id: write
    description: "基于调研写报告"
    depends_on: [research]
    timeout: 300
    workers:
      - id: writer-1
        cli: opencode
        model: opencode/deepseek-v4-flash-free
        system_prompt: |
          你是 writer-1. 基于 {input.findings} 写完整报告.
    # v2: 单 markdown 文件, 用 checks 表达
    deliverable:
      path: data/report.md
      format: markdown
      checks:
        - "## 摘要"                  # scheduler 校验
        - "## 结论"
        - "## 风险评估"
      min_size: 3000

  - id: review
    description: "审核报告"
    depends_on: [write]
    timeout: 300
    workers:
      - id: reviewer-1
        cli: opencode
        system_prompt: |
          你是 reviewer-1. 审核 {input.report}, 决定是否通过.
    # v2: 多文件 + JSON envelope (checks 只描述文件存在)
    deliverable:
      paths:                          # 多文件
        - data/review-comments.json
        - data/review-trail.md
      formats: [json, markdown]
      checks:
        - "review-comments.json"     # 启发式: 文件名 → exists
        - "review-trail.md"
        - "至少 3 条审核意见"        # hint
      schema:                         # 可选: JSON envelope 严格校验
        type: object
        required: [approved, feedback]
        properties:
          approved: {type: boolean}
          feedback: {type: string}
```

**v2 schema 关键设计**:
- `deliverable.path` / `deliverable.paths` / `deliverable.dir` **三选一**, 覆盖单文件 / 多文件 / 文件夹
- `format` / `formats` 是 **hint** (不强制), 支持任意 (md / json / text / html / pdf / ...)
- `checks` 统一列表 — 同时给 worker 提示 + scheduler 校验 (启发式分类)
- `schema` **可选**, 只对结构化 JSON 输出加 (envelope 校验)
- `min_size` / `max_size` scheduler 轻校验 (粗粒度)
- 80% stage **不用写 schema**, 写 `checks` + `min_size` 就够

### 4.2 Pydantic Schema (v2 — 类型化验证)

```python
# src/agents_chat/workflow/schema.py

from pydantic import BaseModel, Field, model_validator
from typing import Literal, Union

class DeliverableSpec(BaseModel):
    """Stage 产出契约 (v2: 灵活 path/paths/dir + checks 合并)."""
    # 路径: 3 选 1
    path: str | None = None          # 单文件, e.g. "data/findings.md"
    paths: list[str] | None = None   # 多文件, e.g. ["a.md", "b.json"]
    dir: str | None = None           # 文件夹, e.g. "data/bundle/"
    # 格式: hint, 不强制
    format: str | None = None         # 单文件时
    formats: list[str] | None = None # 多文件/文件夹时
    # 轻校验 (粗粒度, scheduler 用)
    checks: list[str] = []          # 统一检查列表 (worker 提示 + scheduler 校验)
    min_size: int = 0               # 字符, 避免空文件
    max_size: int | None = None      # 字符, 避免异常大
    # 严格校验 (可选, 对结构化 JSON 输出)
    schema: dict | None = None      # JSON Schema, 校验 envelope 严格结构

    @model_validator(mode="after")
    def check_path_specified(self):
        """path / paths / dir 必须至少 1 个."""
        if not any([self.path, self.paths, self.dir]):
            raise ValueError("deliverable must specify one of: path, paths, dir")
        # 多个互斥
        specified = sum([bool(self.path), bool(self.paths), bool(self.dir)])
        if specified > 1:
            raise ValueError("deliverable.path/paths/dir are mutually exclusive")
        return self


class WorkerSpec(BaseModel):
    """Stage 内 worker 配置 (跟现有 config 兼容)."""
    id: str
    cli: Literal["mock", "opencode", "qwen", "a2a"] = "opencode"
    model: str = "opencode/deepseek-v4-flash-free"
    system_prompt: str = ""


class StageSpec(BaseModel):
    """DAG 节点."""
    id: str = Field(..., regex=r"^[a-z][a-z0-9_-]*$")
    description: str = ""
    depends_on: list[str] = []     # 其他 stage id
    timeout: int = 600              # 秒
    workers: list[WorkerSpec] = Field(..., min_items=1)
    deliverable: DeliverableSpec


class WorkflowSpec(BaseModel):
    """完整 DAG."""
    name: str = Field(..., regex=r"^[a-z][a-z0-9_-]*$")
    description: str = ""
    version: str = "1.0"
    stages: list[StageSpec] = Field(..., min_items=1)

    def topological_order(self) -> list[StageSpec]:
        """拓扑排序, 检测循环依赖."""
        # ... Kahn's algorithm ...
```

### 4.3 `checks` 详解 — 启发式 + 高级 type

**两类表达** (用户友好):

```python
# 简单: 字符串列表 (90% 场景)
checks:
  - "至少 3 个权威来源"        # 启发式: 纯文字 → hint (给 worker 看)
  - "## 结论"                  # 启发式: 含 markdown 标记 → contains (scheduler 校验)
  - "## 来源"

# 高级: dict (10% 场景, 显式 type)
checks:
  - type: hint                  # 只给 worker 看
    value: "至少 3 个来源"
  - type: contains              # 字符串子串
    value: "## 结论"
  - type: contains_any          # 多个子串, 任一匹配
    values: ["done", "complete", "finished"]
  - type: contains_all          # 全部子串必须出现
    values: ["## 结论", "## 来源"]
  - type: min_keywords          # 至少 N 个关键词
    count: 2
    keywords: ["结论", "建议"]
  - type: regex                 # 正则匹配
    pattern: "## 结论.*?\\n"
```

**scheduler 启发式 (简单字符串)**:
```python
# src/agents_chat/workflow/checks.py

def _is_substring_check(text: str) -> bool:
    """启发式: 字符串看起来像 'must contain' 还是 'hint'."""
    # markdown 标记 → contains
    if any(marker in text for marker in ["## ", "# ", "**", "`", "<"]):
        return True
    # 文件路径 (含 .json / .md / .txt) → contains
    if re.search(r"\.\w{2,4}$", text):
        return True
    # 否则 hint
    return False

def evaluate_checks(checks: list[str|dict], file_content: str) -> CheckResult:
    """执行 checks, 返每个的 pass/fail + reason."""
    results = []
    for c in checks:
        if isinstance(c, str):
            # 启发式分类
            if _is_substring_check(c):
                # contains 校验
                results.append({
                    "type": "contains",
                    "expected": c,
                    "passed": c in file_content,
                })
            else:
                # hint, 不校验, 只 log
                results.append({"type": "hint", "value": c, "passed": True})
        else:
            # dict 形式, 显式 type
            t = c.get("type")
            if t == "hint":
                results.append({"type": "hint", "value": c["value"], "passed": True})
            elif t == "contains":
                results.append({"type": "contains", "expected": c["value"], "passed": c["value"] in file_content})
            elif t == "contains_any":
                values = c["values"]
                results.append({"type": "contains_any", "values": values, "passed": any(v in file_content for v in values)})
            elif t == "contains_all":
                values = c["values"]
                results.append({"type": "contains_all", "values": values, "passed": all(v in file_content for v in values)})
            elif t == "min_keywords":
                keywords = c["keywords"]
                count = c["count"]
                found = sum(1 for k in keywords if k in file_content)
                results.append({"type": "min_keywords", "count": count, "found": found, "passed": found >= count})
            elif t == "regex":
                results.append({"type": "regex", "pattern": c["pattern"], "passed": bool(re.search(c["pattern"], file_content, re.DOTALL))})
    return CheckResult(
        all_passed=all(r["passed"] for r in results),
        results=results,
    )
```

### 4.3 YAML 加载 + 验证

```python
# src/agents_chat/workflow/loader.py

import yaml
from pathlib import Path
from .schema import WorkflowSpec

def load_workflow(yaml_path: Path) -> WorkflowSpec:
    """读 YAML → Pydantic 验证 → 拓扑排序 → WorkflowSpec."""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkflowSpec(**raw)
    order = spec.topological_order()  # 抛 ValueError 如果有循环
    return spec
```

---

## 5. 关键设计: Stage 隔离机制

### 5.1 三种方案对比

**方案 A: 私有 channel (推荐)**

```text
Stage "research" 跑:
  - scheduler 创建 data_v2/channels/stage-research-<run_id>.jsonl (私有)
  - 2 个 worker 订阅此 channel
  - 其他 worker 不知道这 channel 存在 (无引用)
  - stage 完, scheduler 删 channel 文件
```

**方案 B: ephemeral socket bus (不推荐)**
- 每个 stage 用临时 UDS socket
- 复杂, 跟现有 channel 重复造轮子

**方案 C: 不隔离 (不推荐)**
- stage 只是"逻辑分组", worker 仍能看到所有 channel
- "前后"靠 ACL 或 routing 限制
- 复杂, 容易出 bug

**我选 A**:
- 复用现有 Channel 机制
- 隔离天然 (其他 worker 不知道 channel 名字)
- 清理简单 (stage 完删文件)

### 5.2 Channel 命名约定

```text
普通 channel:    data_v2/channels/{name}.jsonl         # e.g. fish-market
Stage channel:   data_v2/channels/.stage-{stage_id}-{run_id}.jsonl
                 # 前缀 . 隐藏 (其他 worker 不会订阅)
                 # stage 跑完删文件
```

### 5.3 Worker 怎么"加入" stage

```python
# 在 worker config 注入 stage 信息
# WorkerFactory 检测到 workflow 模式, 给 worker 加:
{
  "agent_id": "researcher-1",
  "mode": "workflow",  # 跟 "passive" / "proactive" 并列
  "subscriptions": [".stage-research-<run_id>"],  # 私有 channel
  "default_channel": ".stage-research-<run_id>",
  "workflow_role": {
    "stage_id": "research",
    "input_from_stage": null,  # 第一个 stage, 无 input
  },
}
```

**关键**: worker 不知道自己是 "stage worker" — 它只知道自己订阅了一个私有 channel, 跟现有 PDR 一样运行。**隔离 = "其他 worker 不知道这 channel"** 而已。

---

## 6. 关键设计: Stage 完成检测

### 6.1 三种方案对比 (v2: checks 轻校验)

**方案 A: Deliverable 文件存在 + checks 轻校验 (推荐, v2)**

```python
def stage_done(stage: StageSpec) -> bool:
    """Stage 完成: deliverable 文件存在 + size + checks 轻校验."""
    # 1. 检查 deliverable (path/paths/dir 三选一)
    if stage.deliverable.path:
        paths_to_check = [stage.deliverable.path]
    elif stage.deliverable.paths:
        paths_to_check = stage.deliverable.paths
    elif stage.deliverable.dir:
        if not (data_dir / stage.deliverable.dir).is_dir():
            return False
        paths_to_check = list((data_dir / stage.deliverable.dir).rglob("*"))
    else:
        return False  # 上面 @model_validator 应该 catch 这个
    
    # 2. 所有路径必须存在
    for p in paths_to_check:
        if not (data_dir / p).exists():
            return False
    
    # 3. Size 检查 (粗粒度, 避免空文件 / 异常大)
    primary_file = data_dir / paths_to_check[0]
    if primary_file.is_file():
        size = primary_file.stat().st_size
        if size < stage.deliverable.min_size:
            return False
        if stage.deliverable.max_size and size > stage.deliverable.max_size:
            return False
    
    # 4. Checks 轻校验 (启发式 + 高级 type, 见 4.3)
    if stage.deliverable.checks and primary_file.is_file():
        try:
            content = primary_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False
        result = evaluate_checks(stage.deliverable.checks, content)
        return result.all_passed  # ← 这里是关键变化, 旧是 "schema validate"
    
    # 5. 可选: 严格 schema (对结构化 JSON 输出)
    if stage.deliverable.schema and primary_file.suffix == ".json":
        import jsonschema
        data = json.loads(primary_file.read_text())
        try:
            jsonschema.validate(data, stage.deliverable.schema)
        except jsonschema.ValidationError:
            return False
    
    return True
```

**方案 B: 显式 `workflow.stage_done()` API**

```python
# worker 跑完调:
from agents_chat.workflow import workflow_api
workflow_api.stage_done("research", data={"sources": [...], ...})
```

- 灵活但要求 worker 主动调
- 容易忘

**方案 C: Channel magic message**

```python
# worker 写一条特殊 channel message:
ch.append(from_=worker_id, content="", metadata={"workflow_stage_done": "research", "deliverable": "data/findings.md"})
```

- 复用 channel 但需要 magic 字段

**我选 A (v2)**:
- 0 改动 (worker 不知道 stage 概念)
- 天然 (文件本身就是契约)
- 可审计 (scheduler 定期 check 文件系统)
- **80% stage 不需要 schema 校验**, 用 checks 启发式就够
- 只有结构化 JSON 输出 (e.g. extract-entities stage) 才写 schema

### 6.2 Scheduler 监控循环 (v2: 多文件支持)

```python
# src/agents_chat/workflow/scheduler.py

import asyncio
from pathlib import Path

class WorkflowScheduler:
    def __init__(self, workflow: WorkflowSpec, data_dir: Path):
        self.workflow = workflow
        self.data_dir = data_dir
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.stage_states: dict[str, str] = {}  # stage_id -> pending/running/done
    
    async def run(self) -> WorkflowResult:
        """主循环: 按拓扑序跑 stage, 监控完成, 转交 deliverable."""
        for stage in self.workflow.topological_order():
            # 1. 启 stage
            await self._start_stage(stage)
            self.stage_states[stage.id] = "running"
            
            # 2. 监控 (等 deliverable + checks 校验 + timeout)
            success = await self._wait_stage_done(stage)
            if not success:
                self.stage_states[stage.id] = "failed"
                return {
                    "status": "failed",
                    "run_id": self.run_id,
                    "failed_stage": stage.id,
                    "check_results": self._last_check_results,  # 返详细失败原因
                }
            
            self.stage_states[stage.id] = "success"
            await self._handoff_deliverable(stage)
            await self._cleanup_stage(stage)
        
        return {"status": "success", "run_id": self.run_id}
    
    async def _start_stage(self, stage: StageSpec):
        """启 stage 的 workers + 创建私有 channel."""
        channel_name = f".stage-{stage.id}-{self.run_id}"
        for worker in stage.workers:
            await self._spawn_worker(worker, stage, channel_name)
        self.stage_states[stage.id] = "running"
    
    async def _wait_stage_done(self, stage: StageSpec) -> bool:
        """等 deliverable 文件 + timeout."""
        deliverable_path = self.data_dir / stage.deliverable.path
        deadline = time.time() + stage.timeout
        while time.time() < deadline:
            if self._check_deliverable(stage):
                return True
            await asyncio.sleep(2)  # 2s poll (Stage 完成不需要 0 延迟, 秒级 OK)
        return False  # timeout
    
    async def _handoff_deliverable(self, stage: StageSpec):
        """把 deliverable 文件 copy 到下游 stage worker 的 workspace."""
        for downstream in self._downstream(stage):
            for worker in downstream.workers:
                workspace = self.data_dir / "workspaces" / worker.id
                # Stage 的 deliverable 变成下游 worker 的 input
                # 写到 workspace/stage_inputs/{upstream_stage_id}.json
                src = self.data_dir / stage.deliverable.path
                dst = workspace / "stage_inputs" / f"{stage.id}.json"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
    
    async def _cleanup_stage(self, stage: StageSpec):
        """Stage 完, 删私有 channel 文件, 保留 deliverable."""
        channel_name = f".stage-{stage.id}-{self.run_id}"
        channel_file = self.data_dir / "channels" / f"{channel_name}.jsonl"
        if channel_file.exists():
            channel_file.unlink()

    def _wait_stage_done(self, stage: StageSpec) -> bool:
        """v2: 用 checks 轻校验 deliverable, 不强 schema."""
        deliverable = stage.deliverable
        
        # 1. 路径收集 (path / paths / dir 三选一)
        if deliverable.path:
            primary_path = self.data_dir / deliverable.path
            all_paths = [primary_path]
        elif deliverable.paths:
            all_paths = [self.data_dir / p for p in deliverable.paths]
            primary_path = all_paths[0]
        elif deliverable.dir:
            dir_path = self.data_dir / deliverable.dir
            if not dir_path.is_dir():
                return False
            all_paths = list(dir_path.rglob("*"))
            primary_path = all_paths[0] if all_paths else None
        else:
            return False
        
        # 2. 所有路径存在
        for p in all_paths:
            if not p.exists():
                return False
        
        # 3. Size 检查
        if primary_path and primary_path.is_file():
            size = primary_path.stat().st_size
            if size < deliverable.min_size:
                return False
            if deliverable.max_size and size > deliverable.max_size:
                return False
        
        # 4. Checks 轻校验
        if deliverable.checks and primary_path and primary_path.is_file():
            try:
                content = primary_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return False
            result = evaluate_checks(deliverable.checks, content)
            self._last_check_results = result
            return result.all_passed
        
        # 5. 可选: 严格 schema (对结构化 JSON)
        if deliverable.schema and primary_path and primary_path.suffix == ".json":
            import jsonschema
            data = json.loads(primary_path.read_text())
            try:
                jsonschema.validate(data, deliverable.schema)
            except jsonschema.ValidationError:
                return False
        
        return True
```

---

## 7. 关键设计: input 转交 (Stage 间数据流)

### 7.1 三种方案对比

**方案 A: 文件 copy 到下游 workspace (推荐)**

```text
Stage research 完, deliverable: data/findings.json
  ↓ scheduler 把 data/findings.json copy 到
  ↓ data/workspaces/writer-1/stage_inputs/research.json
  ↓
Stage write 启, writer-1 启动时:
  - worker config 自动加 "input": "stage_inputs/research.json"
  - worker 在 system_prompt 里引用 {input.findings.claims} 等
```

**方案 B: stdin / env var**

- 跨进程传大数据不方便
- 不灵活

**方案 C: Symlink**

- 节省空间, 但 stage 完 cleanup 复杂

**我选 A**:
- 跟现有 role.md 风格一致 (worker 启动时加载文件)
- 简单, 可调试 (文件就在 workspace 可见)
- 失败易排查 (看 deliverable 文件就知道)

### 7.2 Worker 怎么引用上游 input

**YAML 模板** (在 worker system_prompt 里):

```yaml
- id: writer-1
  system_prompt: |
    你是 writer-1. 基于 {input.findings.claims} 写报告.
    反方观点参考 {input.findings.counterpoints}.
```

**实现**: scheduler 启动 worker 时, 把 stage_inputs 里的文件转成 `{input}` dict, 注入到 system_prompt。

```python
# src/agents_chat/workflow/scheduler.py

def _build_system_prompt(worker: WorkerSpec, stage: StageSpec, upstream_deliverables: dict) -> str:
    """把上游 deliverable 注入 system_prompt."""
    template = worker.system_prompt
    
    # 上游 deliverables 形如:
    # {
    #   "research": {"sources": [...], "claims": [...], "counterpoints": [...]},
    # }
    # 注入后 worker 用 {input.findings.claims} 引用
    
    return template  # 直接用, worker 自己解析 {input.xxx}
```

**注**: 现有 worker 的 LLM 调用 (opencode/qwen) 已经支持 `{input}` 占位符替换。无需新代码。

### 7.3 完整 input 注入示例

```text
Stage "research" 跑完, deliverable:
  data/findings.json = {"sources": [...], "claims": [...], "counterpoints": [...]}

Stage "write" 启动 writer-1:
  1. scheduler copy data/findings.json → data/workspaces/writer-1/stage_inputs/research.json
  2. writer-1 启动, 加载 stage_inputs/research.json 作为 {input}
  3. writer-1 的 system_prompt 模板:
     "你是 writer-1. 基于 {input.claims} 写报告."
  4. LLM 收到 system_prompt, 实际 prompt 包含:
     "你是 writer-1. 基于 [{'a': 1, 'b': 2}, ...] 写报告."
  5. writer-1 写 data/report.md (它的 deliverable)
  6. scheduler 检测 → stage write done → 启 review
```

---

## 8. 失败处理 + 重跑

### 8.1 三种方案

**方案 A: Stage 失败 → workflow 失败, 保留已 done stage, 可手动重跑 (推荐, MVP 简单)**

```bash
# 失败时, 保留已 done stage 的 deliverable
# 重跑整个:
$ python -m agents_chat workflow run pipeline.yaml

# 从指定 stage 重跑:
$ python -m agents_chat workflow run pipeline.yaml --from-stage=write

# 单 stage 重跑:
$ python -m agents_chat workflow run pipeline.yaml --stage=write
```

**方案 B: 自动重试 N 次 (Phase 2)**

```yaml
- id: research
  retry:
    max_attempts: 3
    backoff: exponential  # 1s, 2s, 4s
```

**方案 C: 跳过失败 stage (类似 Kubernetes continueOnError)**

```yaml
- id: research
  on_error: skip  # research 失败不阻塞, write 拿空 input
```

**我选 A**: MVP 简单, 失败易排查, 用户手动重跑可控。

### 8.2 失败处理详细流程

```python
async def _wait_stage_done(self, stage) -> bool:
    """等 deliverable + timeout, 失败分类."""
    deliverable_path = self.data_dir / stage.deliverable.path
    
    # 1. 等文件 (正常路径)
    deadline = time.time() + stage.timeout
    while time.time() < deadline:
        if self._check_deliverable(stage):
            return True
        await asyncio.sleep(2)
    
    # 2. Timeout, 检查 worker 状态
    worker_states = [self._get_worker_state(w.id) for w in stage.workers]
    all_stopped = all(s == "stopped" for s in worker_states)
    
    if all_stopped:
        # 全部 worker 都停了, 但 deliverable 没产出 → stage failed
        return False
    else:
        # 还有 worker 跑着, 但超时了 → kill
        for w in stage.workers:
            self._kill_worker(w.id)
        return False
```

---

## 9. 跟现有架构的集成

### 9.1 复用清单

| 现有组件 | workflow 怎么用 |
|----------|---------------|
| **WorkerFactory** | ✅ stage 启 worker 时复用 `WorkerFactory.create(agent_id, cli_type, data_dir, cli_config)` |
| **CommunicationComponent** | ✅ 100% 复用, 听私有 channel |
| **EventHandler** | ✅ 100% 复用, PDR 4 组件不变 |
| **DecisionMaker** | ✅ 100% 复用 |
| **SessionManager** | ✅ 100% 复用 (每个 stage worker 有独立 session) |
| **CLI adapter** (opencode/qwen/A2A/mock) | ✅ 100% 复用 |
| **Channel** | ✅ 复用, stage channel = 私有 channel (自动创建/删) |
| **Mailbox** | ✅ 复用, 但 stage 内一般不投递 mail (worker 都在同 channel) |
| **EventBus / busd / watchdog** | ✅ 复用, 0 改动 |
| **FastAPI server** | 🆕 +5 个端点 (`/api/workflows`, `/api/workflows/{name}/runs`, ...) |
| **WebUI** | 🆕 +1 个视图 (DAG 状态图, stage 进度) |
| **config.json** (worker 静态配置) | ⚠️ 优先用 YAML workflow; config.json 作为默认 fallback |

### 9.2 REST API 端点 (FastAPI) — 当前实现

**注**: Round 5 实现跟原设计有偏差, 增加了向后兼容的旧端点. 当前共存的端点:

#### 设计文档 §9.2 端点 (推荐用)

```python
# src/agents_chat/infra/server.py

@app.get("/api/workflows/{name}")
async def get_workflow(name: str) -> dict:
    """读 workflow spec (含 stages / workers / deliverable).

    向后兼容: 如果 {name} 是 run_id (runs/ 里存在), 返 run 数据.
    """

@app.post("/api/workflows/{name}/runs")
async def start_workflow_run(name: str, body: dict = {}) -> dict:
    """启新 run by name (body: {from_stage?, single_stage?}).

    body schema (RunByNameRequest):
      from_stage: Optional[str]
      single_stage: Optional[str]
    """

@app.get("/api/workflows/{name}/runs/{run_id}")
async def get_workflow_run(name: str, run_id: str) -> dict:
    """查 run 状态.

    校验: run.workflow_name 必须 == name (防止 leak 别人的 run).
    """

@app.post("/api/workflows/{name}/runs/{run_id}/cancel")
async def cancel_workflow_run(name: str, run_id: str) -> dict:
    """取消 run by name + run_id.

    校验 name 匹配后, 通过 WorkflowRegistry 查 active scheduler 并 cancel.
    """
```

#### 旧端点 (向后兼容)

```python
@app.get("/api/workflows")                  # 旧: 列 runs
@app.get("/api/workflows/{run_id}")         # 旧: 查 run (不带 name)
@app.get("/api/workflows/{run_id}/html")    # 旧: HTML 可视化
@app.post("/api/workflows/run")             # 旧: 启新 run (body: yaml_path)
@app.post("/api/workflows/{run_id}/cancel")  # 旧: 取消 (不带 name)
```

#### 额外端点 (功能扩展)

```python
@app.get("/api/workflows/active")           # 列 active runs
@app.get("/api/workflows/registry")         # 列已注册 workflow YAML (扫盘)
@app.post("/api/workflows/validate")        # 验证 YAML 语法 (不跑)
```

#### 端点 → 设计文档映射

| 设计文档 | 当前实现 | 备注 |
|----------|---------|------|
| `GET /api/workflows` (列已注册) | `GET /api/workflows/registry` | 旧 endpoint 列 runs, 不一致 |
| `GET /api/workflows/{name}` | ✅ `GET /api/workflows/{name}` | 含 backward compat |
| `POST /api/workflows/{name}/runs` | ✅ `POST /api/workflows/{name}/runs` | body 含 from_stage/single_stage |
| `GET /api/workflows/{name}/runs/{run_id}` | ✅ | 校验 name 匹配 |
| `POST /api/workflows/{name}/runs/{run_id}/cancel` | ✅ | 通过 WorkflowRegistry

### 9.3 CLI 命令 — 当前实现

**注**: 跟设计文档略有差异. 当前支持的子命令:

#### 跟设计文档对齐

```bash
# 列出所有已注册 workflow (扫 examples/*.yaml)
$ python -m agents_chat workflow list [--scan-dir DIR] [--data-dir DIR]

# 跑 workflow
$ python -m agents_chat workflow run pipeline.yaml [--from-stage S] [--single-stage S]

# 查 run 状态
$ python -m agents_chat workflow status RUN_ID [--data-dir DIR]
#   注: 设计文档期望 'workflow status research-pipeline run-abc12345'
#   当前只支持 run_id. 设计文档用空格分隔, 实际是单 run_id 参数.

# 取消 run (需 server 启)
$ python -m agents_chat workflow cancel RUN_ID [--server-url URL]
#   注: 设计文档期望 'workflow cancel research-pipeline run-abc12345'
#   当前只支持 run_id (server 端 cancel).
```

#### 额外功能

```bash
# 列所有 run 历史 (跟设计文档的 "list" 不同)
$ python -m agents_chat workflow list-runs [--limit N] [--data-dir DIR]

# 验证 YAML 语法
$ python -m agents_chat workflow validate pipeline.yaml

# 生成 DAG + stage 状态 HTML
$ python -m agents_chat workflow visualize pipeline.yaml [--run-id ID] [-o FILE]

# 列 server 上 active runs
$ python -m agents_chat workflow active [--server-url URL]
```

#### 设计 vs 实现差异

| 设计文档期望 | 当前实现 | 原因 |
|-------------|---------|------|
| `workflow run --stage=research` | `--single-stage research` | 命名差异, 等价功能 |
| `workflow status name run_id` | `workflow status run_id` | 单参数更简单, run_id 唯一 |
| `workflow cancel name run_id` | `workflow cancel run_id` | 同上 |

### 9.4 新增 WebUI 视图

```text
WebUI 现有 6 视图 + 1 新视图 = 7 视图:

  📊 总览 (overview)
  💬 频道详情 (channel)         ← 现有
  🔴 实时聊天 (live-chat)        ← 现有
  🤖 Workers (workers)          ← 现有
  📋 任务 (tasks)               ← 现有
  📥 邮箱 (mailboxes)            ← 现有
  🆕 🌊 Workflow (workflows)    ← 新: DAG 状态图 + stage 进度
```

**新视图内容**:
- DAG mermaid 图 (YAML 自动渲染)
- 每个 stage 状态 (pending/running/success/failed) + 进度条
- 每个 stage 的 deliverable 路径 + 文件大小 + schema 验证状态
- "Re-run from stage" 按钮
- 实时 worker 状态 (在 stage 内)

---

## 10. 实施路线图

### 10.1 1 周 MVP 详细计划

#### Day 1-2: Schema + 解析 (200 行)
```
src/agents_chat/workflow/
├── __init__.py
├── schema.py            # Pydantic: WorkflowSpec, StageSpec, WorkerSpec, DeliverableSpec (~80 行)
└── loader.py           # load_workflow() + topological_order() (~120 行)

tests/unit/workflow/
├── __init__.py
└── test_loader.py      # 5 tests: YAML 解析 / 拓扑 / 循环检测 / 字段验证
```

**Day 1-2 验收**:
- 5 个测试通过 (YAML 解析 / 拓扑 / 循环 / schema 验证)
- 跑 1 个 3-stage YAML, 输出拓扑序

#### Day 3-4: Scheduler 核心 (300 行)
```
src/agents_chat/workflow/
├── scheduler.py         # WorkflowScheduler: run / _start_stage / _wait_stage_done / _handoff / _cleanup (~250 行)
└── runner.py            # _spawn_worker() 调用 WorkerFactory + 启 worker subprocess (~50 行)

tests/unit/workflow/
└── test_scheduler.py   # 6 tests: stage 启动 / 完成检测 / timeout / 重跑 / cleanup
```

**Day 3-4 验收**:
- 跑通 "research → write → review" 3 stage demo
- timeout 正确触发
- deliverable 文件自动 copy 到下游 workspace

#### Day 5: API + CLI (100 行)
```
src/agents_chat/infra/cli/workflow.py   # CLI 子命令 (~80 行)
src/agents_chat/infra/server.py         # +5 个端点 (~50 行)
src/agents_chat/__main__.py            # 注册 workflow 子命令

tests/unit/workflow/
└── test_api.py          # 3 tests
```

**Day 5 验收**:
- `python -m agents_chat workflow run pipeline.yaml` 跑通
- `GET /api/workflows/{name}/runs/{run_id}` 返正确状态

#### Day 6: WebUI + 文档 (200 行)
```
webui/index.html        # +workflow 视图
webui/app.js            # +workflow 逻辑 (DAG 图 + stage 进度)
webui/style.css         # +workflow 样式

docs/26-stage-workflow.md  # 已有 (本文件)
docs/27-workflow-examples.md  # 3 个真实场景示例 (~200 行)
```

**Day 6 验收**:
- WebUI 7 视图 work
- README 更新 + workflow 文档

#### Day 7: E2E 测试 + 收尾
```
tests/unit/workflow/
├── test_e2e.py          # 1 个真 e2e (启 server, 跑 workflow, 验证 deliverable)
```

**Day 7 验收**:
- 真实 workflow run 跑通 3 stage
- 370 + 12 = 382 tests passed

### 10.2 复用率统计

| 模块 | 复用率 | 备注 |
|------|--------|------|
| PDR 4 组件 | 100% | 0 改动 |
| WorkerFactory | 100% | 0 改动, workflow 直接调 |
| Channel | 100% | 0 改动, 复用 `.stage-*` 命名 |
| EventBus / busd / watchdog | 100% | 0 改动 |
| CLI adapter | 100% | 0 改动 |
| FastAPI server | +5% | +5 个端点 |
| WebUI | +1 视图 | DAG 状态图 |
| **新代码** | **~600-800 行** | 全在 `src/agents_chat/workflow/` |

---

## 11. 测试策略

### 11.1 测试矩阵 (12 个新测试)

| 类别 | 数量 | 覆盖 |
|------|------|------|
| `test_loader.py` | 5 | YAML 解析 / 拓扑 / 循环检测 / schema 字段验证 / 字段缺省 |
| `test_scheduler.py` | 6 | stage 启停 / 完成检测 / timeout / 重跑 / cleanup / 状态机 |
| `test_api.py` | 3 | 5 个新端点 / run 状态查询 / cancel |
| `test_e2e.py` | 1 | 真实 workflow run (research → write → review, 验证 deliverable 落盘) |
| **总** | **15** | (12 + 3 backup) |

### 11.2 关键测试示例

```python
# test_e2e.py - 1 个真端到端测试
def test_research_pipeline_runs(tmp_path):
    """跑通 research-pipeline YAML, 验证 3 stage 串行完成."""
    yaml_path = Path(__file__).parent / "fixtures" / "research-pipeline.yaml"
    workflow = load_workflow(yaml_path)
    
    scheduler = WorkflowScheduler(workflow, data_dir=tmp_path)
    result = asyncio.run(scheduler.run())
    
    assert result.status == "success"
    assert (tmp_path / "data" / "findings.json").exists()
    assert (tmp_path / "data" / "report.md").exists()
    assert (tmp_path / "data" / "review-comments.json").exists()
    
    # 验证 deliverable 落到下游 workspace
    assert (tmp_path / "data" / "workspaces" / "writer-1" / "stage_inputs" / "research.json").exists()
```

---

## 12. 风险 + 缓解

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Stage 隔离不彻底 (worker 偷看其他 stage) | 低 | 高 | **不靠 ACL 隔离, 靠 channel 不存在** — 其他 worker 不知道 stage channel 名字就订阅不到 |
| Deliverable schema 验证失败 → workflow 卡住 | 中 | 中 | 默认不强制 schema, 只 warn; 用户可加 `strict: true` |
| Worker 在 stage 内 hang (timeout 不够) | 中 | 中 | timeout 默认 600s, 可在 YAML 调; timeout 后自动 kill worker |
| 大文件 deliverable (e.g. 100MB) | 低 | 中 | copy 而非 symlink (安全但慢); Phase 2 加 `--reference` 模式用 symlink |
| DAG 循环依赖 (research 依赖 write, write 依赖 research) | 低 | 中 | 拓扑排序时 detect, 启动前 raise ValueError |
| 真实 8 个 worker 并发 (stage 内多 worker 协作) | 低 | 低 | 现有 PDR 已有 EventBus/busd 协调, 0 改动 |
| Stage 清理失败 (worker 突然退出) | 中 | 中 | try/finally + 死锁检测 (worker 进程 30s 内退出, scheduler 接管) |

---

## 13. 决策矩阵 (v2 修订后)

| 场景 | 推荐方案 |
|------|----------|
| 一次性多阶段 LLM pipeline | **方案 A** (私有 channel + 文件交付) — 跟现有对齐 |
| 可重复 pipeline (CI/CD 风格) | **方案 A** + 重跑 (`--from-stage`) |
| Stage 隔离 (严格不互见) | **方案 A** (天然, channel 不存在其他 worker 看不到) |
| **Deliverable 是 free text (报告/文案)** | **v2 checks 启发式** (`## 结论` → contains 校验) |
| **Deliverable 是结构化 JSON** | **v2 checks + 可选 schema** (envelope 严格校验) |
| **Deliverable 是多文件 bundle** | **v2 paths:** 列表 + formats: 列表 |
| **Deliverable 是文件夹** | **v2 dir:** 文件夹 + 约定命名 |
| Stage 失败处理 (MVP) | **方案 A** (失败 → 整体 fail, 手动重跑) |
| 真实生产 (10+ 团队用) | 方案 A + 方案 B (retry) — Phase 2 |
| 100% 跟现有对齐 (风格一致) | **方案 A** |

**v2 新增决策** (本期 4 个选择 + 后续):

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| 1 | `checks` 表达 | **合并为单列表** (字符串 + dict 高级) | 简单统一, 启发式分类 |
| 2 | 多文件表达 | **3 种都支持** (path / paths / dir) | 覆盖单文件/多文件/文件夹 |
| 3 | `key_points` vs `must_contain` 区分 | **取消区分, 统一 `checks`** | 启发式自动分类 |
| 4 | 启发式规则 | **含 markdown/html 标记 → contains, 否则 hint** | 90% 场景不需写 type |

---

## 14. 关键设计原则 (无论选哪个方案)

1. **零破坏**: 现有 370 tests 继续过, 现有 channel 模式不变
2. **stage 隔离 = channel 不存在**: 不靠 ACL, 靠物理隔离
3. **deliverable = 文件**: 简单, 可调试, 可审计
4. **scheduler 简化**: 只管 stage 边界, 不管 stage 内部
5. **YAML 优先, config.json 兼容**: workflow 用 YAML, 单 worker 仍用 config.json

---

## 15. 实施细节 (给开发者)

### 15.1 文件结构

```
src/agents_chat/workflow/
├── __init__.py           # 公共导出: load_workflow, WorkflowScheduler
├── schema.py             # Pydantic models (~80 行)
├── loader.py             # YAML → WorkflowSpec + 拓扑排序 (~120 行)
├── scheduler.py          # WorkflowScheduler: run / stage lifecycle (~250 行)
└── runner.py             # _spawn_worker + 启 worker subprocess (~50 行)

src/agents_chat/infra/
├── cli/workflow.py       # CLI 子命令 (~80 行)
└── server.py             # +5 个 /api/workflows 端点 (~50 行)

webui/
├── index.html            # +workflow 视图 (~30 行)
├── app.js                # +workflow 逻辑 (~150 行)
└── style.css             # +workflow 样式 (~50 行)

tests/unit/workflow/
├── __init__.py
├── test_loader.py        # 5 tests
├── test_scheduler.py     # 6 tests
├── test_api.py           # 3 tests
└── test_e2e.py           # 1 test
└── fixtures/
    └── research-pipeline.yaml  # 测试用真实 YAML

docs/
└── 26-stage-workflow.md  # 本文档
└── 27-workflow-examples.md  # 3 个真实场景示例 (Phase 2)
```

### 15.2 关键代码示例

```python
# src/agents_chat/workflow/scheduler.py (核心)

import asyncio
import shutil
import time
from pathlib import Path
from .schema import WorkflowSpec

class WorkflowScheduler:
    def __init__(self, spec: WorkflowSpec, data_dir: Path):
        self.spec = spec
        self.data_dir = data_dir
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.stage_states: dict[str, str] = {}  # stage_id -> state
    
    async def run(self) -> dict:
        """主循环."""
        for stage in self.spec.topological_order():
            print(f"[workflow {self.run_id}] starting stage '{stage.id}'")
            await self._start_stage(stage)
            self.stage_states[stage.id] = "running"
            
            success = await self._wait_stage_done(stage)
            if not success:
                self.stage_states[stage.id] = "failed"
                return {
                    "status": "failed",
                    "run_id": self.run_id,
                    "failed_stage": stage.id,
                }
            
            self.stage_states[stage.id] = "success"
            await self._handoff_deliverable(stage)
            await self._cleanup_stage(stage)
            print(f"[workflow {self.run_id}] stage '{stage.id}' done")
        
        return {"status": "success", "run_id": self.run_id}
    
    async def _start_stage(self, stage):
        """启 stage workers + 创建私有 channel."""
        from agents_chat.infra.worker_factory import WorkerFactory
        channel_name = f".stage-{stage.id}-{self.run_id}"
        for worker_spec in stage.workers:
            cfg = {
                "cli": worker_spec.cli,
                "cli_config": {"model": worker_spec.model} if worker_spec.model else {},
                "system_prompt": worker_spec.system_prompt,
                "default_channel": channel_name,
                "subscriptions": [channel_name],
                "data_dir": str(self.data_dir),
                "mode": "proactive",  # stage 内 worker 主动协调
            }
            await self._spawn_worker(worker_spec.id, cfg)
    
    async def _wait_stage_done(self, stage) -> bool:
        """等 deliverable 文件 + timeout."""
        deliverable_path = self.data_dir / stage.deliverable.path
        deadline = time.time() + stage.timeout
        while time.time() < deadline:
            if deliverable_path.exists() and self._validate_deliverable(stage):
                return True
            await asyncio.sleep(2)
        return False
    
    def _validate_deliverable(self, stage) -> bool:
        """验证 deliverable 符合 schema (可选)."""
        if not stage.deliverable.schema:
            return True
        deliverable_path = self.data_dir / stage.deliverable.path
        try:
            data = json.loads(deliverable_path.read_text())
            import jsonschema
            jsonschema.validate(data, stage.deliverable.schema)
            return True
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            print(f"[workflow] schema validation failed: {e}")
            return False
    
    async def _handoff_deliverable(self, stage):
        """把 deliverable 复制到下游 stage 的 worker workspace."""
        for downstream in self.spec.stages:
            if stage.id not in downstream.depends_on:
                continue
            src = self.data_dir / stage.deliverable.path
            for w in downstream.workers:
                workspace = self.data_dir / "workspaces" / w.id
                dst = workspace / "stage_inputs" / f"{stage.id}.json"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"[workflow] handoff {stage.id} → {w.id} ({dst.relative_to(self.data_dir)})")
    
    async def _cleanup_stage(self, stage):
        """删私有 channel, 保留 deliverable."""
        channel_file = self.data_dir / "channels" / f".stage-{stage.id}-{self.run_id}.jsonl"
        if channel_file.exists():
            channel_file.unlink()
```

```python
# src/agents_chat/workflow/loader.py (核心)

import yaml
from pathlib import Path
from .schema import WorkflowSpec, StageSpec

def load_workflow(yaml_path: Path) -> WorkflowSpec:
    """读 YAML → Pydantic 验证 → 拓扑排序."""
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    spec = WorkflowSpec(**raw)
    # 拓扑排序 (顺便检测循环)
    topo = _topological_sort(spec.stages)
    return spec


def _topological_sort(stages: list[StageSpec]) -> list[StageSpec]:
    """Kahn's algorithm, 返排序后 list. 有循环抛 ValueError."""
    by_id = {s.id: s for s in stages}
    in_degree = {s.id: 0 for s in stages}
    for s in stages:
        for dep in s.depends_on:
            if dep not in by_id:
                raise ValueError(f"stage '{s.id}' depends on unknown stage '{dep}'")
            in_degree[s.id] += 1
    
    queue = [sid for sid, deg in in_degree.items() if deg == 0]
    order = []
    while queue:
        sid = queue.pop(0)
        order.append(by_id[sid])
        for s in stages:
            if sid in s.depends_on:
                in_degree[s.id] -= 1
                if in_degree[s.id] == 0:
                    queue.append(s.id)
    
    if len(order) != len(stages):
        raise ValueError(f"cycle detected in workflow stages")
    return order
```

```python
# src/agents_chat/infra/cli/workflow.py (CLI)

import argparse
import asyncio
from pathlib import Path
from agents_chat.workflow.loader import load_workflow
from agents_chat.workflow.scheduler import WorkflowScheduler

def main(args: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="agents-chat-channel workflow runner")
    sub = parser.add_subparsers(dest="cmd", required=True)
    
    p_run = sub.add_parser("run", help="Run a workflow")
    p_run.add_argument("yaml", type=Path, help="Workflow YAML file")
    p_run.add_argument("--data-dir", type=Path, default=Path("./data_v2"))
    p_run.add_argument("--from-stage", type=str, default=None, help="Start from this stage")
    p_run.add_argument("--stage", type=str, default=None, help="Run only this stage")
    
    p_list = sub.add_parser("list", help="List available workflows")
    p_list.add_argument("--yaml-dir", type=Path, default=Path("./workflows"))
    
    p_status = sub.add_parser("status", help="Check run status")
    p_status.add_argument("workflow_name")
    p_status.add_argument("run_id")
    
    p_cancel = sub.add_parser("cancel", help="Cancel a run")
    p_cancel.add_argument("workflow_name")
    p_cancel.add_argument("run_id")
    
    args = parser.parse_args(args)
    
    if args.cmd == "run":
        spec = load_workflow(args.yaml)
        scheduler = WorkflowScheduler(spec, args.data_dir, from_stage=args.from_stage, single_stage=args.stage)
        result = asyncio.run(scheduler.run())
        print(f"Workflow finished: {result}")
    elif args.cmd == "list":
        # 扫 .yaml 文件
        ...
    elif args.cmd == "status":
        # 查 /api/workflows/{name}/runs/{run_id}
        ...
    elif args.cmd == "cancel":
        # 调 /api/workflows/{name}/runs/{run_id}/cancel
        ...
```

### 15.3 跟现有 server 集成

```python
# src/agents_chat/infra/server.py (新增 ~50 行)

@app.get("/api/workflows")
async def list_workflows():
    """扫 data_v2/workflows/*.yaml 返 list."""
    yaml_dir = data_dir / "workflows"
    return [
        {"name": p.stem, "path": str(p.relative_to(data_dir))}
        for p in yaml_dir.glob("*.yaml")
    ]

@app.post("/api/workflows/{name}/runs")
async def start_workflow_run(name: str, body: dict = Body({})):
    """启新 run."""
    yaml_path = data_dir / "workflows" / f"{name}.yaml"
    spec = load_workflow(yaml_path)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 启动后台 task
    asyncio.create_task(_run_workflow_async(spec, data_dir, run_id))
    return {"run_id": run_id, "status": "started", "name": name}

async def _run_workflow_async(spec, data_dir, run_id):
    """后台 task 跑 workflow."""
    scheduler = WorkflowScheduler(spec, data_dir, run_id=run_id)
    result = await scheduler.run()
    # 写 run 状态到 data_v2/runs/{run_id}.json (持久化)
    (data_dir / "runs" / f"{run_id}.json").write_text(json.dumps(result))

@app.get("/api/workflows/{name}/runs/{run_id}")
async def get_workflow_run(name: str, run_id: str):
    """读 runs/{run_id}.json."""
    run_file = data_dir / "runs" / f"{run_id}.json"
    if not run_file.exists():
        raise HTTPException(404, f"run {run_id} not found")
    return json.loads(run_file.read_text())
```

### 15.4 WebUI 7 视图

```javascript
// webui/app.js (新增 ~150 行, workflow 视图逻辑)

class WorkflowView {
    constructor() {
        this.container = $('workflow-view');
    }
    
    async render() {
        // 1. 列 workflow
        const workflows = await api('/api/workflows');
        // 2. mermaid 图渲染
        for (const wf of workflows) {
            const spec = await api(`/api/workflows/${wf.name}`);
            this.renderMermaidGraph(spec);
        }
        // 3. 列 run + 状态
        for (const wf of workflows) {
            const runs = await api(`/api/workflows/${wf.name}/runs`);
            this.renderRunsList(wf, runs);
        }
    }
    
    renderMermaidGraph(spec) {
        // 渲染 DAG: stage 节点 + 依赖箭头
        const mermaid = `graph LR\n${
            spec.stages.map(s => 
                `  ${s.id}[${s.id}: ${s.status || 'pending'}]`
            ).join('\n')
        }\n${
            spec.stages.flatMap(s => 
                s.depends_on.map(dep => `  ${dep} --> ${s.id}`)
            ).join('\n')
        }`;
        // 调 mermaid.render() (CDN)
    }
    
    renderRunsList(workflow, runs) {
        // 表格: run_id, started_at, status, 各 stage 状态
    }
    
    async reRunFromStage(workflowName, stageId) {
        await api(`/api/workflows/${workflowName}/runs`, {
            method: 'POST',
            body: { from_stage: stageId }
        });
    }
}
```

---

## 16. 实施细节 (Phase 2 路线图, 暂不做)

| 任务 | 工作量 | 价值 |
|------|--------|------|
| **自动重试** (stage retry + backoff) | 1 天 | 提高成功率 |
| **HITL** (Human-in-the-loop stage) | 2 天 | 关键审核场景 |
| **DAG 版本化** (workflow 历史) | 1 天 | 审计 + 回滚 |
| **多 DAG 依赖** (DAG A 完触发 DAG B) | 1 天 | 复杂工作流 |
| **时间触发** (cron-like schedule) | 1 天 | 周期任务 |
| **Workflow 模板库** (GitHub Actions Marketplace 风格) | 1 天 | 复用 |
| **Slack/Email 告警** (stage 失败通知) | 半天 | 运维 |

---

## 17. 参考资源

### 内部
- [docs/15-v2-architecture-overview.md](15-v2-architecture-overview.md) — 现有架构
- [docs/21-event-driven-bus.md](21-event-driven-bus.md) — 3 层事件驱动
- [docs/22-uds-bus.md](22-uds-bus.md) — UDS busd
- [docs/23-a2a-research.md](23-a2a-research.md) — A2A 调研
- [docs/24-a2a-client.md](24-a2a-client.md) — A2AClient 集成

### 外部 (DAG 工作流参考)
- **Apache Airflow**: https://airflow.apache.org/ — 工业 DAG 调度器
- **Prefect**: https://www.prefect.io/ — 现代 DAG 调度器, Python DSL
- **Temporal**: https://temporal.io/ — 持久化工作流
- **GitHub Actions**: https://docs.github.com/actions — YAML workflow, 简单
- **LangGraph**: https://langchain-ai.github.io/langgraph/ — 节点 + 边, in-process

### 对比文章
- Airflow vs Prefect vs Temporal: https://www.prefect.io/guides/airflow-vs-prefect-vs-temporal
- LangGraph vs AutoGen: https://blog.langchain.dev/langgraph-vs-autogen/

---

## 18. 结论

**调研结论**: 用户想要的"stage 隔离 + 文件交付" DAG 编排**跟现有架构**正交, 通过**复用现有组件** + **新增 workflow 模块** 0 破坏地实现。

**推荐方案**: **方案 A (私有 channel + 文件交付)**, 4 个决策都选 A:
- 1 周工作量
- 100% 复用现有组件 (PDR / Channel / CLI / EventBus)
- 0 破坏 (370 tests 继续过)
- 自然 stage 隔离 (channel 不存在其他 worker 看不到)
- 简单失败处理 (MVP: 失败 → 整体 fail + 手动重跑)

**下一步**: 等用户 review 文档后, 我按 1 周路线图开干。
- 告诉我"开干", 我立即实施 Day 1-2 (schema + loader)
- 或者"再调整", 我们迭代设计

**价值评估**:
- **作为项目**: 把"一次性多阶段 LLM pipeline" 变成"可重复 + 可审计 + 可观测" 的生产级能力
- **作为 portfolio**: 跟 A2A client-only + 3 层事件驱动 + UDS busd 形成完整的"现代 multi-agent 平台" 技术栈
- **差异化**: 跟 Airflow/Prefect (传统 ETL) 比 LLM-native, 跟 LangGraph (单进程) 比跨进程 + 复用现有架构

**完成路径**: ~5 轮 git commits, 跟之前的 A2A/event-driven/UDS 节奏一致。
