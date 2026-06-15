"""
Workflow loader 测试.

覆盖:
  1. test_load_simple_workflow — 3 stage YAML 解析
  2. test_topological_order — stage 依赖排序
  3. test_cycle_detection — 循环依赖报错
  4. test_unknown_dependency — 未知 stage id 报错
  5. test_pydantic_validation — 字段缺/错报错
  6. test_path_paths_dir_exclusive (v2) — path/paths/dir 互斥
  7. test_path_required (v2) — 必须三选一
  8. test_no_self_dependency (v2) — stage 不能依赖自己
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agents_chat.workflow import load_workflow
from agents_chat.workflow.loader import load_workflow_from_string
from agents_chat.workflow.schema import StageSpec, WorkflowSpec


# =============================================================================
# Test 1: 简单 3 stage workflow
# =============================================================================


class TestLoadSimpleWorkflow:
    def test_load_3_stage_yaml(self, tmp_path):
        """3 stage YAML 解析正确."""
        yaml_content = """
name: research-pipeline
description: 调研 → 写 → 审核
version: "1.0"

stages:
  - id: research
    description: 调研
    workers:
      - id: researcher-1
        cli: opencode
    deliverable:
      path: data/findings.md
      format: markdown
      checks: ["## 结论", "## 来源"]
      min_size: 1000
  
  - id: write
    description: 写报告
    depends_on: [research]
    workers:
      - id: writer-1
        cli: opencode
    deliverable:
      path: data/report.md
      format: markdown
  
  - id: review
    description: 审核
    depends_on: [write]
    workers:
      - id: reviewer-1
        cli: opencode
    deliverable:
      path: data/review.json
      format: json
      schema:
        type: object
        required: [approved]
        properties:
          approved: {type: boolean}
"""
        yaml_file = tmp_path / "pipeline.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        spec = load_workflow(yaml_file)
        assert spec.name == "research-pipeline"
        assert spec.description == "调研 → 写 → 审核"
        assert spec.version == "1.0"
        assert len(spec.stages) == 3

        # 检查 stage 字段
        research = spec.get_stage("research")
        assert research is not None
        assert research.timeout == 600  # 默认
        assert research.deliverable.path == "data/findings.md"
        assert research.deliverable.checks == ["## 结论", "## 来源"]
        assert research.deliverable.min_size == 1000

    def test_load_from_string(self):
        spec = load_workflow_from_string(_SIMPLE_YAML)
        assert spec.name == "research-pipeline"
        assert len(spec.stages) == 3


# =============================================================================
# Test 2: 拓扑排序
# =============================================================================


class TestTopologicalOrder:
    def test_linear_chain(self):
        """线性链: A → B → C → D 排好序."""
        spec = load_workflow_from_string(_LINEAR_YAML)
        order = spec.topological_order()
        ids = [s.id for s in order]
        assert ids == ["a", "b", "c", "d"], f"got {ids}"

    def test_diamond_dependency(self):
        """菱形依赖: A → {B, C} → D."""
        yaml = """
name: diamond
stages:
  - id: d
    depends_on: [b, c]
    workers: [{id: w1}]
    deliverable: {path: out/d.json}
  - id: c
    depends_on: [a]
    workers: [{id: w1}]
    deliverable: {path: out/c.json}
  - id: b
    depends_on: [a]
    workers: [{id: w1}]
    deliverable: {path: out/b.json}
  - id: a
    workers: [{id: w1}]
    deliverable: {path: out/a.json}
"""
        spec = load_workflow_from_string(yaml)
        order = spec.topological_order()
        ids = [s.id for s in order]
        # a 必须在 b/c 前, d 必须在最后
        assert ids[0] == "a"
        assert ids[-1] == "d"
        assert ids.index("b") < ids.index("d")
        assert ids.index("c") < ids.index("d")

    def test_no_dependencies(self):
        """无依赖的 stage, 字典序排."""
        yaml = """
name: parallel
stages:
  - id: z-stage
    workers: [{id: w}]
    deliverable: {path: out/z.json}
  - id: a-stage
    workers: [{id: w}]
    deliverable: {path: out/a.json}
  - id: m-stage
    workers: [{id: w}]
    deliverable: {path: out/m.json}
"""
        spec = load_workflow_from_string(yaml)
        order = spec.topological_order()
        ids = [s.id for s in order]
        # 入度 0 的 stage 按字典序排
        assert ids == ["a-stage", "m-stage", "z-stage"]

    def test_downstream_stages(self):
        """downstream_stages() 返所有依赖此 stage 的 stage."""
        spec = load_workflow_from_string(_LINEAR_YAML)
        # a 的下游是 b
        downstream = spec.downstream_stages("a")
        assert [s.id for s in downstream] == ["b"]
        # b 的下游是 c
        downstream = spec.downstream_stages("b")
        assert [s.id for s in downstream] == ["c"]


# =============================================================================
# Test 3: 循环检测
# =============================================================================


class TestCycleDetection:
    def test_direct_cycle(self):
        """直接循环: A → B → A. load_workflow 内部已 detect."""
        yaml = """
name: cycle
stages:
  - id: a
    depends_on: [b]
    workers: [{id: w}]
    deliverable: {path: out/a.json}
  - id: b
    depends_on: [a]
    workers: [{id: w}]
    deliverable: {path: out/b.json}
"""
        with pytest.raises(ValueError, match="cycle"):
            load_workflow_from_string(yaml)

    def test_indirect_cycle(self):
        """间接循环: A → B → C → A."""
        yaml = """
name: cycle
stages:
  - id: a
    depends_on: [c]
    workers: [{id: w}]
    deliverable: {path: out/a.json}
  - id: b
    depends_on: [a]
    workers: [{id: w}]
    deliverable: {path: out/b.json}
  - id: c
    depends_on: [b]
    workers: [{id: w}]
    deliverable: {path: out/c.json}
"""
        with pytest.raises(ValueError, match="cycle"):
            load_workflow_from_string(yaml)


# =============================================================================
# Test 4: 未知依赖
# =============================================================================


class TestUnknownDependency:
    def test_unknown_stage_id(self):
        """依赖不存在的 stage. load_workflow 内部已 detect."""
        yaml = """
name: typo
stages:
  - id: a
    depends_on: [nonexistent-stage]
    workers: [{id: w}]
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValueError, match="unknown stage 'nonexistent-stage'"):
            load_workflow_from_string(yaml)


# =============================================================================
# Test 5: Pydantic 验证
# =============================================================================


class TestPydanticValidation:
    def test_missing_required_field(self):
        """stage 缺 workers 字段."""
        yaml = """
name: invalid
stages:
  - id: a
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError, match="workers"):
            load_workflow_from_string(yaml)

    def test_empty_workers(self):
        """workers 是空 list (Pydantic min_items=1 报错)."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: []
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError):
            load_workflow_from_string(yaml)

    def test_invalid_stage_id(self):
        """stage id 含大写 (regex 限制)."""
        yaml = """
name: invalid
stages:
  - id: InvalidStage
    workers: [{id: w}]
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError, match="stage id 'InvalidStage' must match"):
            load_workflow_from_string(yaml)

    def test_invalid_worker_id(self):
        """worker id 含特殊字符."""
        yaml = """
name: invalid
stages:
  - id: a
    workers:
      - id: "worker@1"
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError, match="worker id 'worker@1' must match"):
            load_workflow_from_string(yaml)

    def test_duplicate_stage_ids(self):
        """两个 stage 同 id 报错."""
        yaml = """
name: invalid
stages:
  - id: dup
    workers: [{id: w}]
    deliverable: {path: out/1.json}
  - id: dup
    workers: [{id: w}]
    deliverable: {path: out/2.json}
"""
        with pytest.raises(ValidationError, match="duplicate stage ids"):
            load_workflow_from_string(yaml)

    def test_duplicate_worker_ids(self):
        """stage 内两个 worker 同 id 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers:
      - id: same
      - id: same
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError, match="duplicate worker ids"):
            load_workflow_from_string(yaml)


# =============================================================================
# Test 6: path/paths/dir 互斥 (v2 新增)
# =============================================================================


class TestDeliverablePathExclusive:
    def test_only_path(self):
        yaml = """
name: ok
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      path: data/findings.md
"""
        spec = load_workflow_from_string(yaml)
        paths = spec.stages[0].deliverable.get_all_paths()
        assert paths == ["data/findings.md"]

    def test_only_paths(self):
        yaml = """
name: ok
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      paths: [data/a.md, data/b.json]
"""
        spec = load_workflow_from_string(yaml)
        paths = spec.stages[0].deliverable.get_all_paths()
        assert paths == ["data/a.md", "data/b.json"]

    def test_only_dir(self):
        yaml = """
name: ok
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      dir: data/bundle/
"""
        spec = load_workflow_from_string(yaml)
        assert spec.stages[0].deliverable.get_all_paths() == ["data/bundle/"]

    def test_path_and_paths_exclusive(self):
        """path + paths 同时给 → 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      path: data/a.md
      paths: [data/b.md]
"""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            load_workflow_from_string(yaml)

    def test_path_and_dir_exclusive(self):
        """path + dir 同时给 → 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      path: data/a.md
      dir: data/bundle/
"""
        with pytest.raises(ValidationError, match="mutually exclusive"):
            load_workflow_from_string(yaml)

    def test_paths_lengths_mismatch_with_formats(self):
        """paths 跟 formats 长度不一致 → 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      paths: [a.md, b.md, c.md]
      formats: [markdown, json]
"""
        with pytest.raises(ValidationError, match="same length"):
            load_workflow_from_string(yaml)


# =============================================================================
# Test 7: 必须三选一
# =============================================================================


class TestDeliverablePathRequired:
    def test_no_path_specified(self):
        """path/paths/dir 都不给 → 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      format: markdown
      checks: ["## 结论"]
"""
        with pytest.raises(ValidationError, match="must specify one of"):
            load_workflow_from_string(yaml)

    def test_empty_paths_list(self):
        """paths 是空 list (当 path 没给, dirs 没给) → 报错."""
        yaml = """
name: invalid
stages:
  - id: a
    workers: [{id: w}]
    deliverable:
      paths: []
"""
        with pytest.raises(ValidationError, match="must specify one of"):
            load_workflow_from_string(yaml)


# =============================================================================
# Test 8: stage 不能依赖自己
# =============================================================================


class TestNoSelfDependency:
    def test_self_dependency(self):
        yaml = """
name: invalid
stages:
  - id: a
    depends_on: [a]
    workers: [{id: w}]
    deliverable: {path: out/a.json}
"""
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            load_workflow_from_string(yaml)


# =============================================================================
# Fixtures
# =============================================================================


_SIMPLE_YAML = """
name: research-pipeline
description: 调研 → 写 → 审核
version: "1.0"

stages:
  - id: research
    workers:
      - id: researcher-1
        cli: opencode
    deliverable:
      path: data/findings.md
      format: markdown
      checks: ["## 结论", "## 来源"]
      min_size: 1000

  - id: write
    depends_on: [research]
    workers:
      - id: writer-1
        cli: opencode
    deliverable:
      path: data/report.md
      format: markdown

  - id: review
    depends_on: [write]
    workers:
      - id: reviewer-1
        cli: opencode
    deliverable:
      path: data/review.json
      format: json
"""


_LINEAR_YAML = """
name: linear
stages:
  - id: a
    workers: [{id: w1}]
    deliverable: {path: out/a.json}
  - id: b
    depends_on: [a]
    workers: [{id: w1}]
    deliverable: {path: out/b.json}
  - id: c
    depends_on: [b]
    workers: [{id: w1}]
    deliverable: {path: out/c.json}
  - id: d
    depends_on: [c]
    workers: [{id: w1}]
    deliverable: {path: out/d.json}
"""
