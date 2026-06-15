"""
Workflow Loader — YAML → WorkflowSpec + 拓扑排序.

设计文档: docs/26-stage-workflow.md 章节 4.3

Usage:
    spec = load_workflow(Path("./workflows/pipeline.yaml"))
    for stage in spec.topological_order():
        print(stage.id)
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .schema import WorkflowSpec


def load_workflow(yaml_path: str | Path) -> WorkflowSpec:
    """读 YAML → Pydantic 验证 → 拓扑排序 → WorkflowSpec.

    Args:
        yaml_path: workflow YAML 文件路径

    Returns:
        WorkflowSpec: 验证过的 DAG (含拓扑序)

    Raises:
        FileNotFoundError: YAML 不存在
        yaml.YAMLError: YAML 解析失败
        pydantic.ValidationError: schema 验证失败 (字段缺/错)
        ValueError: 拓扑错误 (未知依赖 / 循环)
    """
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"workflow YAML not found: {yaml_path}")

    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"workflow YAML must be a mapping (got {type(raw).__name__})"
        )

    # Pydantic 验证 (字段缺/错/格式不对 → ValidationError)
    spec = WorkflowSpec(**raw)

    # 拓扑排序 (顺带检测循环 + 未知依赖 → ValueError)
    order = spec.topological_order()
    if not order:
        raise ValueError(f"workflow {spec.name} has no stages")

    return spec


def load_workflow_from_string(yaml_content: str) -> WorkflowSpec:
    """从字符串读 YAML (测试用, 或从 stdin 读).

    Raises:
        pydantic.ValidationError: schema 验证失败
        ValueError: 拓扑错误
    """
    raw = yaml.safe_load(yaml_content)
    if not isinstance(raw, dict):
        raise ValueError(
            f"workflow YAML must be a mapping (got {type(raw).__name__})"
        )
    spec = WorkflowSpec(**raw)
    order = spec.topological_order()
    if not order:
        raise ValueError("workflow has no stages")
    return spec


def write_workflow_yaml(spec: WorkflowSpec, yaml_path: str | Path) -> None:
    """把 WorkflowSpec 序列化成 YAML 写出 (debug / 测试用).

    字段顺序: name → description → version → stages
    """
    import json

    yaml_path = Path(yaml_path)
    data = spec.model_dump(exclude_none=True)
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
