"""
Workflow Schema — Pydantic v2 models for DAG workflow.

设计文档: docs/26-stage-workflow.md 章节 4.2

核心抽象:
  - WorkflowSpec     # 完整 DAG
  - StageSpec        # DAG 节点 (1+ worker + 1 deliverable)
  - WorkerSpec       # Stage 内 worker 配置
  - DeliverableSpec  # Stage 产出契约 (v2: path/paths/dir + checks)

v2 schema 关键设计:
  - path/paths/dir 三选一 (model_validator 互斥)
  - format 是 hint, 不强制
  - checks 统一列表 (字符串启发式 + dict 高级 type)
  - strict schema 降级为可选 (90% stage 不写)
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


# ID 验证: 跟 stage/worker id 一致, 小写/大写 + 数字 + -_
_ID_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


# =============================================================================
# Worker 配置
# =============================================================================


class WorkerSpec(BaseModel):
    """Stage 内 worker 配置 (跟现有 config 兼容).

    字段对应 WorkerFactory.create() 的参数:
      - id:          worker_id
      - cli:         cli_type (mock / opencode / qwen / a2a)
      - model:       cli_config.model
      - system_prompt: cli_config.system_prompt (或 role 内容)
    """
    id: str = Field(..., min_length=1, max_length=64)
    cli: Literal["mock", "opencode", "qwen", "a2a"] = "opencode"
    model: str = ""   # 空 = 用 CLI 默认模型
    system_prompt: str = ""

    @model_validator(mode="after")
    def _validate_id(self):
        if not _ID_PATTERN.match(self.id):
            raise ValueError(
                f"worker id '{self.id}' must match {_ID_PATTERN.pattern}"
            )
        return self


# =============================================================================
# Deliverable 配置 (v2: path/paths/dir + checks)
# =============================================================================


# Check item: 字符串 (启发式) 或 dict (显式 type)
CheckItem = Union[str, dict]


class DeliverableSpec(BaseModel):
    """Stage 产出契约.

    v2 设计:
      - path / paths / dir 三选一 (互斥, model_validator)
      - format / formats: hint, 不强制
      - checks: 统一检查列表 (字符串启发式 + dict 高级)
      - min_size / max_size: scheduler 轻校验
      - json_schema: 可选 strict JSON Schema (YAML key: schema)
    """
    model_config = {"populate_by_name": True}
    # ===== 必填: 路径 (3 选 1) =====
    path: Optional[str] = None       # 单文件, e.g. "data/findings.md"
    paths: Optional[list[str]] = None # 多文件, e.g. ["a.md", "b.json"]
    dir: Optional[str] = None        # 文件夹, e.g. "data/bundle/"

    # ===== 可选: 格式 hint =====
    format: Optional[str] = None      # 单文件, e.g. "markdown"
    formats: Optional[list[str]] = None  # 多文件, e.g. ["markdown", "json"]

    # ===== 轻校验 (粗粒度) =====
    checks: list[CheckItem] = []     # 统一检查列表
    min_size: int = 0               # 字符, 避免空文件
    max_size: Optional[int] = None   # 字符, 避免异常大

    # ===== 严格校验 (可选) =====
    json_schema: Optional[dict] = Field(
        None,
        alias="schema",  # YAML 中仍用 schema: 键名
        description="JSON Schema for envelope validation (YAML key: schema)",
    )

    @model_validator(mode="after")
    def _validate_path_exclusive(self):
        """path/paths/dir 三选一, 不能多个共存."""
        specified = sum([
            self.path is not None,
            self.paths is not None and len(self.paths) > 0,
            self.dir is not None,
        ])
        if specified == 0:
            raise ValueError(
                "deliverable must specify one of: path, paths, dir"
            )
        if specified > 1:
            raise ValueError(
                f"deliverable.path/paths/dir are mutually exclusive "
                f"(got {specified} specified)"
            )
        return self

    @model_validator(mode="after")
    def _validate_path_format(self):
        """paths 跟 formats 列表长度一致 (如果都给)."""
        if self.paths and self.formats:
            if len(self.paths) != len(self.formats):
                raise ValueError(
                    f"deliverable.paths ({len(self.paths)} items) and "
                    f"formats ({len(self.formats)} items) must have same length"
                )
        return self

    def get_all_paths(self) -> list[str]:
        """返所有 deliverable 路径 (扁平化 path/paths/dir)."""
        if self.path:
            return [self.path]
        if self.paths:
            return list(self.paths)
        if self.dir:
            # 文件夹: 实际路径在 stage 跑时才能 enumerate, 返 dir 自身
            return [self.dir]
        return []

    def get_primary_path(self) -> Optional[str]:
        """返主校验路径 (单文件 / paths[0] / dir 自身)."""
        if self.path:
            return self.path
        if self.paths:
            return self.paths[0]
        if self.dir:
            return self.dir
        return None


# =============================================================================
# Stage 配置
# =============================================================================


class RetrySpec(BaseModel):
    """Stage 重试配置.

    例:
      retry:
        max_attempts: 3           # 最多 3 次 (含首次)
        backoff: exponential       # fixed / exponential
        initial_delay: 5.0         # 首次重试前等 5s
        max_delay: 60.0            # 防止指数爆炸
    """
    max_attempts: int = Field(1, ge=1, le=10)
    backoff: Literal["none", "fixed", "exponential"] = "none"
    initial_delay: float = Field(5.0, ge=0.0, le=600.0)
    max_delay: float = Field(60.0, ge=1.0, le=3600.0)


class StageSpec(BaseModel):
    """DAG 节点 (v2 schema).

    字段:
      - id: stage 唯一 id
      - description: 人类可读
      - depends_on: 其他 stage id 列表 (DAG 边)
      - timeout: 整个 stage 最长秒数
      - workers: stage 内 worker 列表 (>= 1)
      - deliverable: stage 产出契约
    """
    id: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    depends_on: list[str] = []       # 其他 stage id
    timeout: int = 600              # 秒, 默认 10 分钟
    workers: list[WorkerSpec] = Field(..., min_length=1)
    deliverable: DeliverableSpec
    retry: RetrySpec = Field(default_factory=RetrySpec)  # 重试配置 (默认不重试)

    @model_validator(mode="after")
    def _validate_id(self):
        if not _ID_PATTERN.match(self.id):
            raise ValueError(
                f"stage id '{self.id}' must match {_ID_PATTERN.pattern}"
            )
        return self

    @model_validator(mode="after")
    def _validate_worker_ids_unique(self):
        """stage 内 worker id 不重复."""
        ids = [w.id for w in self.workers]
        if len(ids) != len(set(ids)):
            from collections import Counter
            dupes = [k for k, v in Counter(ids).items() if v > 1]
            raise ValueError(
                f"stage '{self.id}' has duplicate worker ids: {dupes}"
            )
        return self

    @model_validator(mode="after")
    def _validate_no_self_dependency(self):
        if self.id in self.depends_on:
            raise ValueError(
                f"stage '{self.id}' cannot depend on itself"
            )
        return self


# =============================================================================
# Workflow 配置
# =============================================================================


class WorkflowSpec(BaseModel):
    """完整 DAG (v2).

    字段:
      - name: workflow 唯一 id
      - description: 人类可读
      - version: semver-like string
      - stages: stage 列表 (>= 1)
    """
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    version: str = "1.0"
    stages: list[StageSpec] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _validate_name(self):
        if not _ID_PATTERN.match(self.name):
            raise ValueError(
                f"workflow name '{self.name}' must match {_ID_PATTERN.pattern}"
            )
        return self

    @model_validator(mode="after")
    def _validate_unique_stage_ids(self):
        from collections import Counter
        ids = [s.id for s in self.stages]
        if len(ids) != len(set(ids)):
            dupes = [k for k, v in Counter(ids).items() if v > 1]
            raise ValueError(f"workflow has duplicate stage ids: {dupes}")
        return self

    def get_stage(self, stage_id: str) -> Optional[StageSpec]:
        """按 id 查 stage."""
        for s in self.stages:
            if s.id == stage_id:
                return s
        return None

    def topological_order(self) -> list[StageSpec]:
        """Kahn's algorithm 拓扑排序.

        Returns:
            排序后的 stage 列表 (依赖在前, 被依赖在后)

        Raises:
            ValueError: 未知依赖 或 循环依赖
        """
        by_id = {s.id: s for s in self.stages}
        # 校验所有依赖都存在
        for s in self.stages:
            for dep in s.depends_on:
                if dep not in by_id:
                    raise ValueError(
                        f"stage '{s.id}' depends on unknown stage '{dep}'"
                    )

        # Kahn's algorithm
        in_degree: dict[str, int] = {s.id: 0 for s in self.stages}
        for s in self.stages:
            in_degree[s.id] = len(s.depends_on)

        # 初始化: 入度 0 的 stage
        ready = sorted([sid for sid, deg in in_degree.items() if deg == 0])
        order: list[StageSpec] = []
        while ready:
            sid = ready.pop(0)
            order.append(by_id[sid])
            # 找哪些 stage 依赖 sid
            for s in self.stages:
                if sid in s.depends_on:
                    in_degree[s.id] -= 1
                    if in_degree[s.id] == 0:
                        # 插入保持字典序 (确定性)
                        ready.append(s.id)
                        ready.sort()

        if len(order) != len(self.stages):
            remaining = [sid for sid, deg in in_degree.items() if deg > 0]
            raise ValueError(
                f"cycle detected in workflow stages: {remaining} have unresolved deps"
            )
        return order

    def downstream_stages(self, stage_id: str) -> list[StageSpec]:
        """返所有依赖 stage_id 的下游 stage (依赖此 stage 完成的)."""
        return [s for s in self.stages if stage_id in s.depends_on]


# =============================================================================
# Check 执行结果
# =============================================================================


class CheckItem(BaseModel):
    """单条 check 的执行结果."""
    raw: Union[str, list, dict]   # 原始 check (字符串 / 列表 / 字典)
    type: str                      # hint | contains | contains_any | contains_all | min_keywords | regex
    passed: bool
    detail: str = ""              # 详细说明 (e.g. "found 2/3 keywords")
    value: Any = None             # 期望值 (debug 用, 可以是 list)


class CheckResult(BaseModel):
    """一组 check 的执行结果."""
    items: list[CheckItem]
    all_passed: bool

    def failed_items(self) -> list[CheckItem]:
        """返失败的 check (含 hint 永远 pass, 所以这里只有 contains 类)."""
        return [i for i in self.items if not i.passed]
