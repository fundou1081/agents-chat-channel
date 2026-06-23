"""
WorkflowScheduler — DAG 编排 + 文件交付主引擎.

设计文档: docs/26-stage-workflow.md 章节 6 + 7 + 8

核心职责:
  1. 拓扑排序 (Kahn's algorithm, 跟 schema 共享)
  2. 按序跑 stage (spawn workers + 监控 + cleanup)
  3. stage 完成检测 (v2 checks + size)
  4. stage 间文件转交 (deliverable → 下游 workspace/stage_inputs/)
  5. 失败处理 (timeout + kill workers)
  6. 状态持久化 (写到 data_v2/runs/<run_id>.json)
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .checks import evaluate_checks
from .runner import (
    build_channel_name,
    build_input_handoff_paths,
    collect_upstream_inputs,
    spawn_stage_workers,
)
from .schema import StageSpec, WorkflowSpec

logger = logging.getLogger("workflow-scheduler")


class WorkflowRunResult:
    """单次 run 的最终结果."""

    def __init__(
        self,
        workflow_name: str,
        run_id: str,
        status: str = "running",     # running / success / failed / canceled
        started_at: Optional[str] = None,
        finished_at: Optional[str] = None,
        failed_stage: Optional[str] = None,
        stage_states: Optional[dict[str, str]] = None,  # stage_id -> state
        check_results: Optional[dict[str, dict]] = None,  # stage_id -> CheckResult
        stage_deps: Optional[dict[str, list[str]]] = None,  # stage_id -> [depends_on]
    ):
        self.workflow_name = workflow_name
        self.run_id = run_id
        self.status = status
        self.started_at = started_at or datetime.now(timezone.utc).isoformat()
        self.finished_at = finished_at
        self.failed_stage = failed_stage
        self.stage_states = stage_states or {}
        self.check_results = check_results or {}
        self.stage_deps = stage_deps or {}

    def to_dict(self) -> dict:
        return {
            "workflow_name": self.workflow_name,
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "failed_stage": self.failed_stage,
            "stage_states": self.stage_states,
            "stage_deps": self.stage_deps,
            "check_results": {
                sid: {
                    "all_passed": r.all_passed,
                    "items": [
                        {
                            "type": i.type,
                            "passed": i.passed,
                            "detail": i.detail,
                            "value": str(i.value) if i.value is not None else None,
                        }
                        for i in r.items
                    ],
                }
                for sid, r in self.check_results.items()
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class WorkflowScheduler:
    """DAG 编排主引擎.

    Usage:
        spec = load_workflow(Path("pipeline.yaml"))
        scheduler = WorkflowScheduler(spec, data_dir=Path("./data_v2"))
        result = await scheduler.run()
    """

    def __init__(
        self,
        workflow: WorkflowSpec,
        data_dir: Path | str,
        run_id: Optional[str] = None,
        from_stage: Optional[str] = None,
        single_stage: Optional[str] = None,
        poll_interval: float = 2.0,
        spawn_delay: float = 0.5,
    ):
        """Args:
            workflow: WorkflowSpec (已 load + 验证)
            data_dir: agents_chat data_dir
            run_id: 跑 ID (默认生成)
            from_stage: 从哪个 stage 开始 (跳过前面 stage)
            single_stage: 只跑单个 stage (用于 --stage 重跑)
            poll_interval: deliverable poll 间隔 (秒, default 2.0)
            spawn_delay: worker 启动后到 polling 间的等待 (秒, default 0.5)
        """
        self.workflow = workflow
        self.data_dir = Path(data_dir).resolve()
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:8]}"
        self.from_stage = from_stage
        self.single_stage = single_stage
        self.poll_interval = poll_interval
        self.spawn_delay = spawn_delay

        # 状态
        self.result = WorkflowRunResult(
            workflow_name=workflow.name,
            run_id=self.run_id,
        )

        # Stage 状态
        self._stage_agents: dict[str, list] = {}  # stage_id -> [Agent]
        self._stage_tasks: dict[str, list[asyncio.Task]] = {}  # stage_id -> [asyncio.Task]
        self._stage_check_results: dict[str, object] = {}  # stage_id -> CheckResult

        # 私有 channel 文件路径 (stage 完删)
        self._stage_channels: dict[str, Path] = {}
        # 取消标志 (在 cancel() 设置)
        self._cancel_requested: bool = False
        # 每个 stage 的重试次数 (key=stage_id, value=int)
        self._stage_retry_attempts: dict[str, int] = {}

    # =================================================================
    # 主循环
    # =================================================================

    async def run(self) -> WorkflowRunResult:
        """主循环: 按拓扑序跑 stage."""
        logger.info(
            f"[workflow {self.run_id}] starting '{self.workflow.name}'"
        )
        try:
            # 1. 计算要跑的 stage 列表
            all_stages = self.workflow.topological_order()
            stages_to_run = self._filter_stages(all_stages)
            logger.info(
                f"[workflow {self.run_id}] stages to run: "
                f"{[s.id for s in stages_to_run]}"
            )

            # 2. 按序跑
            upstream_deliverables: list[tuple[str, Path]] = []  # [(stage_id, path), ...]
            for stage in stages_to_run:
                logger.info(
                    f"[workflow {self.run_id}] === stage '{stage.id}' ==="
                )
                self.result.stage_states[stage.id] = "running"

                # 2a. 启 stage workers (注入 upstream input)
                try:
                    await self._start_stage(stage, upstream_deliverables)
                except Exception as e:
                    logger.error(
                        f"[workflow {self.run_id}] stage '{stage.id}' "
                        f"failed to start: {e}"
                    )
                    self.result.stage_states[stage.id] = "failed"
                    self.result.failed_stage = stage.id
                    self.result.status = "failed"
                    return self._finalize()

                # 2b. 监控 stage 完成 (等 deliverable + checks + timeout)
                success = await self._wait_stage_done(stage)

                # 检查 cancel
                if self._check_cancel():
                    logger.info(
                        f"[workflow {self.run_id}] cancel detected after stage '{stage.id}'"
                    )
                    self.result.stage_states[stage.id] = "canceled"
                    self._cleanup_stage(stage)
                    return self._finalize()

                if not success:
                    # 失败: 考虑 retry
                    attempts = self._stage_retry_attempts.get(stage.id, 0)
                    max_attempts = stage.retry.max_attempts
                    if attempts + 1 < max_attempts:
                        # 还能重试
                        delay = self._compute_retry_delay(stage, attempts)
                        logger.warning(
                            f"[workflow {self.run_id}] stage '{stage.id}' failed "
                            f"(attempt {attempts + 1}/{max_attempts}), "
                            f"retrying in {delay:.1f}s"
                        )
                        self._stage_retry_attempts[stage.id] = attempts + 1
                        self._cleanup_stage(stage)
                        await asyncio.sleep(delay)
                        # 不记入 upstream_deliverables, 不设 stage_states
                        # 重新进 stage loop (用 continue 跳到下一次)
                        continue
                    # 重试用完
                    logger.error(
                        f"[workflow {self.run_id}] stage '{stage.id}' failed "
                        f"after {attempts + 1} attempt(s)"
                    )
                    self.result.stage_states[stage.id] = "failed"
                    self.result.failed_stage = stage.id
                    self.result.status = "failed"
                    return self._finalize()

                # 2c. Stage 完: 记录 deliverable, cleanup
                self.result.stage_states[stage.id] = "success"
                primary_path = self._get_deliverable_primary_path(stage)
                if primary_path:
                    upstream_deliverables.append((stage.id, primary_path))
                self._cleanup_stage(stage)

                # single_stage 模式: 跑完一个就 exit
                if self.single_stage:
                    break

            # 3. 全部 stage done
            self.result.status = "success"
            logger.info(f"[workflow {self.run_id}] all stages done")
            return self._finalize()

        except Exception as e:
            logger.exception(f"[workflow {self.run_id}] unhandled error: {e}")
            self.result.status = "failed"
            return self._finalize()

    def _finalize(self) -> WorkflowRunResult:
        """清理 + 持久化 + 返 result.

        注: 不 await cancelled tasks, 因为 agent.run() 可能不立即响应 cancel.
        依赖 _cleanup_stage 提前 stop agents + cancel tasks 防止泄漏.
        """
        # 清理所有仍在跑的 worker (保险)
        for stage_id, tasks in self._stage_tasks.items():
            for t in tasks:
                if not t.done():
                    logger.warning(
                        f"[workflow {self.run_id}] cancelling leftover task "
                        f"for stage {stage_id}"
                    )
                    t.cancel()
        for stage_id, agents in self._stage_agents.items():
            for a in agents:
                try:
                    a.stop()
                except Exception:
                    pass

        # 持久化
        self.result.finished_at = datetime.now(timezone.utc).isoformat()
        self.result.check_results = dict(self._stage_check_results)
        self.result.stage_deps = {
            s.id: list(s.depends_on) for s in self.workflow.stages
        }
        try:
            self._save_run_state()
        except Exception as e:
            logger.error(
                f"[workflow {self.run_id}] failed to save run state: {e}"
            )
        return self.result

    def cancel(self) -> None:
        """取消当前 run. 后续 stage 跳过, 已 running 的 stage 全部 cleanup.

        调后:
          - _cancel_requested = True
          - 当前 stage (如在跑) 会被 _wait_stage_done 检测到后 return False
          - 下一轮循环: status='canceled', 跳出

        注: 此方法是设置一个 flag, 在 run() 循环检查.
            不是强行中断 (agent.run() task 异步在跑, cancel 后台自然结束).
        """
        if self._cancel_requested:
            return  # 幂等
        logger.info(f"[workflow {self.run_id}] cancel requested")
        self._cancel_requested = True
        self.result.status = "canceled"
        # 立即 stop 当前 stage 的 agents (要 force terminate)
        for agents in self._stage_agents.values():
            for a in agents:
                try:
                    a.stop()
                except Exception:
                    pass

    def _check_cancel(self) -> bool:
        """检查是否请求取消. 返 True = 取消."""
        return self._cancel_requested

    # =================================================================
    # Stage lifecycle
    # =================================================================

    def _filter_stages(
        self, all_stages: list[StageSpec]
    ) -> list[StageSpec]:
        """根据 from_stage / single_stage 过滤.

        Raises:
            ValueError: from_stage 或 single_stage 不存在
        """
        if self.single_stage:
            # 只跑一个 stage
            for s in all_stages:
                if s.id == self.single_stage:
                    return [s]
            raise ValueError(
                f"stage '{self.single_stage}' not found in workflow"
            )
        if self.from_stage:
            # 从 from_stage 开始 (含 from_stage)
            started = False
            filtered = []
            for s in all_stages:
                if s.id == self.from_stage:
                    started = True
                if started:
                    filtered.append(s)
            if not started:
                # from_stage 不存在 → 显式报错, 避免静默返空
                raise ValueError(
                    f"from_stage '{self.from_stage}' not found in workflow"
                )
            return filtered
        return all_stages

    def _post_stage_task(
        self,
        stage: StageSpec,
        channel_name: str,
        upstream_deliverables: list[tuple[str, Path]],
    ) -> None:
        """Post [TASK ...] broadcast to private stage channel.

        告诉 stage 内 workers:
          - 任务 ID (run_id-stage_id)
          - stage system_prompt (任务内容)
          - upstream deliverable 路径 (如需读上游产出)
        """
        from ..infra.files.channel import Channel

        channel_path = self.data_dir / "channels" / f"{channel_name}.jsonl"
        channel_path.parent.mkdir(parents=True, exist_ok=True)
        if not channel_path.exists():
            channel_path.touch()
        ch = Channel(channel_path, channel_name)

        # 构造任务内容
        task_id = f"{self.run_id}-{stage.id}"
        upstream_info = ""
        if upstream_deliverables:
            upstream_lines = [
                f"- {sid}: {p}" for sid, p in upstream_deliverables
            ]
            upstream_info = "\n\n上游 deliverable (可读):\n" + "\n".join(upstream_lines)

        # 1st worker 的 system_prompt 作为任务描述
        task_content = stage.workers[0].system_prompt if stage.workers else f"执行 stage '{stage.id}'"
        task_content += upstream_info
        task_content += (
            f"\n\n产出文件 (相对路径): {stage.deliverable.path or (stage.deliverable.paths[0] if stage.deliverable.paths else None) or stage.deliverable.dir or 'output'}"
        )

        # 提到所有 worker (each one receives the mention)
        mentions = [w.id for w in stage.workers]

        # 算 deliverable 绝对路径 (agent cwd = workspace, 不是 data_dir)
        deliverable_rel_path = (
            stage.deliverable.path
            or (stage.deliverable.paths[0] if stage.deliverable.paths else None)
            or stage.deliverable.dir
        )
        abs_deliverable = (
            str(self.data_dir / deliverable_rel_path) if deliverable_rel_path else "(无)"
        )

        # 在 task_content 加绝对路径提示 (不动 system_prompt)
        task_content += f"\n\n【重要】产出文件必须写到: {abs_deliverable}"

        ch.append(
            from_="workflow",
            content=task_content,
            type="task_broadcast",
            task_id=task_id,
            mentions=mentions,
        )
        logger.info(
            f"[workflow {self.run_id}] posted task '{task_id}' to {channel_name} "
            f"with mentions {mentions}"
        )

    async def _start_stage(
        self,
        stage: StageSpec,
        upstream_deliverables: list[tuple[str, Path]],
    ) -> None:
        """启 stage 内 workers + 创建私有 channel."""
        channel_name = build_channel_name(stage.id, self.run_id)
        self._stage_channels[stage.id] = (
            self.data_dir / "channels" / f"{channel_name}.jsonl"
        )

        # Handoff: 复制上游 deliverable 到当前 stage 的 worker workspace
        self._handoff_to_stage(stage, upstream_deliverables)

        # Spawn workers (用 WorkerFactory)
        agents = await asyncio.to_thread(
            spawn_stage_workers,
            stage,
            self.data_dir,
            channel_name,
            upstream_deliverables,
        )
        self._stage_agents[stage.id] = agents

        # 启 agent.run() 后台 task
        tasks = []
        for agent in agents:
            task = asyncio.create_task(
                agent.run(),
                name=f"workflow-{self.run_id}-{stage.id}-{agent.agent_id}",
            )
            tasks.append(task)
        self._stage_tasks[stage.id] = tasks

        # Post task broadcast to private channel so workers know what to do
        # (agents listen on channel for mention/task_broadcast messages)
        await asyncio.to_thread(
            self._post_stage_task,
            stage, channel_name, upstream_deliverables,
        )

        # 给 worker 一点启动时间
        await asyncio.sleep(self.spawn_delay)
        logger.info(
            f"[workflow {self.run_id}] stage '{stage.id}' started "
            f"({len(agents)} workers in {channel_name})"
        )

    async def _wait_stage_done(self, stage: StageSpec) -> bool:
        """等 deliverable + checks 校验 + timeout. 返 True = done."""
        deliverable = stage.deliverable
        deadline = time.time() + stage.timeout

        # 计算校验路径列表
        paths_to_check = self._get_all_deliverable_paths(stage)
        if not paths_to_check:
            logger.error(
                f"[workflow {self.run_id}] stage '{stage.id}' has no deliverable path"
            )
            return False

        # Poll 循环
        poll_interval = self.poll_interval
        while time.time() < deadline:
            # 1. 路径存在
            if all(p.exists() for p in paths_to_check):
                # 2. Size 检查
                primary = self._get_deliverable_primary_path(stage)
                if primary and primary.is_file():
                    size = primary.stat().st_size
                    if size < deliverable.min_size:
                        logger.warning(
                            f"[workflow {self.run_id}] deliverable {primary} "
                            f"size {size} < min_size {deliverable.min_size}"
                        )
                        await asyncio.sleep(poll_interval)
                        continue
                    if deliverable.max_size and size > deliverable.max_size:
                        logger.warning(
                            f"[workflow {self.run_id}] deliverable {primary} "
                            f"size {size} > max_size {deliverable.max_size}"
                        )
                        await asyncio.sleep(poll_interval)
                        continue

                # 3. Checks 校验 (v2)
                if primary and primary.is_file() and deliverable.checks:
                    try:
                        content = primary.read_text(encoding="utf-8", errors="replace")
                    except Exception as e:
                        logger.warning(f"read {primary} failed: {e}")
                        await asyncio.sleep(poll_interval)
                        continue
                    check_result = evaluate_checks(deliverable.checks, content)
                    self._stage_check_results[stage.id] = check_result
                    if not check_result.all_passed:
                        failed = check_result.failed_items()
                        # 非阻塞: deliverable 存在 + size 满足, checks 是建议性验证
                        logger.warning(
                            f"[workflow {self.run_id}] stage '{stage.id}' "
                            f"checks not all passed (non-blocking): "
                            f"{[(i.type, i.detail) for i in failed]}"
                        )

                # 4. 可选 strict JSON schema (对 envelope)
                if primary and primary.is_file() and deliverable.json_schema:
                    if primary.suffix == ".json":
                        try:
                            import jsonschema
                            data = json.loads(primary.read_text())
                            jsonschema.validate(data, deliverable.json_schema)
                        except ImportError:
                            logger.error(
                                f"[workflow {self.run_id}] jsonschema not installed, "
                                f"skipping schema validation for {primary}"
                            )
                            # jsonschema 未安装 → 跳过, 不阻塞 pipeline
                        except Exception as e:
                            # 非阻塞: schema mismatch 是 hints, 不阻 pipeline
                            logger.warning(
                                f"[workflow {self.run_id}] schema validation "
                                f"failed (non-blocking): {e}"
                            )

                # 全部 pass (size + 文件存在 + checks 警告已 log), stage done
                logger.info(
                    f"[workflow {self.run_id}] stage '{stage.id}' deliverable OK"
                )
                return True

            # 还没 done, 等
            await asyncio.sleep(poll_interval)

        # Timeout
        logger.error(
            f"[workflow {self.run_id}] stage '{stage.id}' timeout after {stage.timeout}s"
        )
        return False

    def _handoff_to_stage(
        self,
        stage: StageSpec,
        upstream_deliverables: list[tuple[str, Path]],
    ) -> None:
        """把上游 deliverable 复制到当前 stage 的 worker workspace.

        在 _start_stage 时调用，确保下游 stage 的 worker 启动前有完整的
        stage_inputs/ 目录（含所有上游阶段的产出）。
        """
        if not upstream_deliverables:
            return
        handoff_paths = build_input_handoff_paths(
            stage, upstream_deliverables, self.data_dir
        )
        for upstream_id, dst in handoff_paths:
            # 找对应 src
            src = None
            for sid, p in upstream_deliverables:
                if sid == upstream_id:
                    src = p
                    break
            if not src or not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
                logger.info(
                    f"[workflow {self.run_id}] handoff {upstream_id} → "
                    f"{dst.relative_to(self.data_dir)}"
                )
            except Exception as e:
                logger.warning(
                    f"[workflow {self.run_id}] handoff {upstream_id} → {dst} failed: {e}"
                )

    def _compute_retry_delay(self, stage: StageSpec, attempt: int) -> float:
        """算 retry 前的等待秒数. attempt = 0 表示第 1 次失败, 准备第 2 次."""
        r = stage.retry
        if r.backoff == "none":
            return 0.0
        if r.backoff == "fixed":
            delay = r.initial_delay
        else:  # exponential
            delay = r.initial_delay * (2 ** attempt)
        return min(delay, r.max_delay)

    def _cleanup_stage(self, stage: StageSpec) -> None:
        """删私有 channel 文件, 保留 deliverable."""
        # 删 channel 文件
        channel_file = self._stage_channels.pop(stage.id, None)
        agents = self._stage_agents.pop(stage.id, [])
        tasks = self._stage_tasks.pop(stage.id, [])

        if channel_file and channel_file.exists():
            try:
                channel_file.unlink()
                logger.debug(
                    f"[workflow {self.run_id}] cleaned up channel {channel_file}"
                )
            except OSError as e:
                logger.warning(
                    f"[workflow {self.run_id}] cleanup channel failed: {e}"
                )

        # Stop agents (graceful)
        for agent in agents:
            try:
                agent.stop()
            except Exception:
                pass

        # Cancel tasks
        for task in tasks:
            if not task.done():
                task.cancel()

    # =================================================================
    # Helpers
    # =================================================================

    def _get_all_deliverable_paths(self, stage: StageSpec) -> list[Path]:
        """返 stage deliverable 的所有绝对路径."""
        d = stage.deliverable
        paths: list[Path] = []
        if d.path:
            paths.append(self.data_dir / d.path)
        elif d.paths:
            for p in d.paths:
                paths.append(self.data_dir / p)
        elif d.dir:
            dir_path = self.data_dir / d.dir
            if dir_path.is_dir():
                paths.extend(dir_path.rglob("*"))
            else:
                # dir 不存在: 返 dir 自身, scheduler 等它出现
                paths.append(dir_path)
        return paths

    def _get_deliverable_primary_path(
        self, stage: StageSpec
    ) -> Optional[Path]:
        """返主校验路径 (单文件 / paths[0] / dir 自身)."""
        d = stage.deliverable
        if d.path:
            return self.data_dir / d.path
        if d.paths:
            return self.data_dir / d.paths[0]
        if d.dir:
            return self.data_dir / d.dir
        return None

    def _save_run_state(self) -> None:
        """持久化 run 状态到 data_v2/runs/<run_id>.json."""
        runs_dir = self.data_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        run_file = runs_dir / f"{self.run_id}.json"
        run_file.write_text(
            self.result.to_json(),
            encoding="utf-8",
        )
        logger.info(
            f"[workflow {self.run_id}] run state saved to {run_file}"
        )
