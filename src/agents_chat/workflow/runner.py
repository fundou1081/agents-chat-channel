"""
Workflow Runner — spawn stage workers via WorkerFactory.

跟现有架构集成:
  - WorkerFactory.create() 同步创建 Agent (init_workspace, cli 等)
  - 我们在 asyncio loop 里 agent.run() 启后台 task
  - stage 完 / 失败, agent.stop() 清理
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from .schema import StageSpec, WorkerSpec

logger = logging.getLogger("workflow-runner")


def build_system_prompt(
    template: str,
    upstream_inputs: Optional[dict[str, Any]] = None,
) -> str:
    """把上游 deliverable 注入 system_prompt 模板.

    模板支持 {input.<key>} 占位符, 替换为 upstream_inputs[<key>].
    未匹配的占位符保留 (worker 看到, 自己报错).
    """
    if not template or not upstream_inputs:
        return template

    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in upstream_inputs:
            value = upstream_inputs[key]
            return str(value)
        return match.group(0)  # 保留未匹配占位符

    return re.sub(r"\{input\.(\w+)\}", replacer, template)


def collect_upstream_inputs(
    upstream_deliverables: list[tuple[str, Path]],
) -> dict[str, Any]:
    """从上游 deliverable 文件加载 input dict.

    假设 deliverable 是 JSON 文件 (envelope).
    """
    import json
    inputs: dict[str, Any] = {}
    for stage_id, path in upstream_deliverables:
        if not path.exists():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            # 尝试 parse JSON
            data = json.loads(content)
            # 支持两种 envelope:
            #   - { "key": value, ... }  → 直接 spread
            #   - { "stage_id": {key: value, ...} } → nested
            if isinstance(data, dict):
                # 检查是不是 { stage_id: {...} } 形式
                if stage_id in data and isinstance(data[stage_id], dict):
                    inputs.update(data[stage_id])
                else:
                    # spread flat
                    inputs.update(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                f"failed to load upstream deliverable {path} for stage {stage_id}: {e}"
            )
    return inputs


def spawn_stage_workers(
    stage: StageSpec,
    data_dir: Path,
    channel_name: str,
    upstream_deliverables: list[tuple[str, Path]] | None = None,
) -> list:
    """创建 stage 内 workers (通过 WorkerFactory).

    Args:
        stage: stage spec (含 workers 列表)
        data_dir: agents_chat data_dir
        channel_name: 私有 channel 名 (e.g. ".stage-research-<run_id>")
        upstream_deliverables: [(upstream_stage_id, deliverable_path), ...]

    Returns:
        list[Agent]: 创建的 agent 实例 (尚未 run, 需 scheduler 启 asyncio task)
    """
    from agents_chat.infra.worker_factory import WorkerFactory

    upstream_inputs = collect_upstream_inputs(upstream_deliverables or [])
    agents = []
    seen_ids = set()
    for w in stage.workers:
        if w.id in seen_ids:
            # WorkerFactory 也会校验, 这里早返
            raise ValueError(f"duplicate worker id in stage: {w.id}")
        seen_ids.add(w.id)

        # system_prompt 注入 upstream input
        system_prompt = build_system_prompt(w.system_prompt, upstream_inputs)

        # WorkerFactory.create (同步, 返回 Agent 实例)
        agent = WorkerFactory.create(
            agent_id=w.id,
            cli_type=w.cli,
            data_dir=data_dir,
            mode="proactive",
            subscriptions=[channel_name],
            default_channel=channel_name,
            cli_config={"model": w.model} if w.model else {},
            system_prompt=system_prompt,
            init_workspace=True,
        )
        agents.append(agent)
        logger.info(
            f"spawned worker {w.id} (cli={w.cli}, model={w.model}) "
            f"in stage '{stage.id}' channel={channel_name}"
        )
    return agents


def build_channel_name(stage_id: str, run_id: str) -> str:
    """构造私有 channel 名 (跟现有 channel 命名约定)."""
    return f".stage-{stage_id}-{run_id}"


def build_input_handoff_paths(
    stage: StageSpec,
    upstream_deliverables: list[tuple[str, Path]],
    data_dir: Path,
) -> list[tuple[str, Path]]:
    """构造下游 stage worker 的 stage_inputs 路径.

    Returns:
        [(upstream_stage_id, copy_to_path), ...]
        scheduler 用这些路径把 deliverable 复制到下游 worker workspace
    """
    handoff = []
    for upstream_id, src in upstream_deliverables:
        for w in stage.workers:
            dst = data_dir / "workspaces" / w.id / "stage_inputs" / f"{upstream_id}.json"
            handoff.append((upstream_id, dst))
    return handoff
