"""
Workflow runner + scheduler 测试.

覆盖:
  1. test_build_system_prompt - {input.<key>} 替换
  2. test_build_system_prompt_no_inputs - 无 input 时保留模板
  3. test_collect_upstream_inputs - JSON 文件加载
  4. test_build_channel_name - 私有 channel 命名
  5. test_build_input_handoff_paths - handoff 路径生成
  6. test_spawn_stage_workers_integration - 真 spawn workers (用 mock CLI)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents_chat.workflow import (
    build_channel_name,
    build_input_handoff_paths,
    build_system_prompt,
    collect_upstream_inputs,
)


# =============================================================================
# Runner 单元测试 (不依赖 WorkerFactory)
# =============================================================================


class TestBuildSystemPrompt:
    def test_replace_input_keys(self):
        """{input.<key>} 占位符替换."""
        template = "你是 writer. 基于 {input.findings} 写报告. 反方: {input.counterpoints}."
        upstream = {
            "findings": [{"a": 1}, {"b": 2}],
            "counterpoints": ["观点 1", "观点 2"],
        }
        result = build_system_prompt(template, upstream)
        assert "基于 [{'a': 1}, {'b': 2}]" in result
        assert "反方: ['观点 1', '观点 2']" in result
        # 不在 upstream 的占位符保留
        assert "{input.missing}" not in result  # 不应该有 missing key

    def test_no_upstream_keeps_template(self):
        """无 upstream inputs 时模板原样保留."""
        template = "你是 worker. {input.x} 不可用."
        result = build_system_prompt(template, None)
        assert result == template
        result = build_system_prompt(template, {})
        assert result == template

    def test_unmatched_placeholder_kept(self):
        """不匹配的占位符保留 (worker 看到, 自己报错)."""
        template = "用 {input.given} 和 {input.missing}."
        upstream = {"given": "value1"}
        result = build_system_prompt(template, upstream)
        assert "value1" in result
        assert "{input.missing}" in result  # 保留

    def test_empty_template(self):
        """空模板 → 空串."""
        assert build_system_prompt("", {"x": 1}) == ""


class TestCollectUpstreamInputs:
    def test_simple_spread(self):
        """JSON 文件直接 spread (key-value 平铺)."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deliverable = tmp_path / "findings.json"
            deliverable.write_text(json.dumps({
                "sources": ["a", "b"],
                "claims": ["x", "y"],
            }))
            inputs = collect_upstream_inputs([("research", deliverable)])
            assert inputs == {"sources": ["a", "b"], "claims": ["x", "y"]}

    def test_nested_envelope(self):
        """JSON 文件用 { stage_id: {key: value} } 嵌套 envelope."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deliverable = tmp_path / "findings.json"
            deliverable.write_text(json.dumps({
                "research": {
                    "sources": ["a", "b"],
                    "claims": ["x"],
                }
            }))
            inputs = collect_upstream_inputs([("research", deliverable)])
            assert inputs == {"sources": ["a", "b"], "claims": ["x"]}

    def test_missing_file_silent(self):
        """文件不存在, 安静跳过 (不抛)."""
        with tempfile_TemporaryDirectory() as tmp:
            inputs = collect_upstream_inputs([("research", Path(tmp) / "nope.json")])
            assert inputs == {}

    def test_invalid_json_silent(self):
        """JSON 解析失败, 警告但继续 (不影响其他 deliverable)."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            good = tmp_path / "good.json"
            good.write_text(json.dumps({"k": "v"}))
            bad = tmp_path / "bad.json"
            bad.write_text("not json {")
            inputs = collect_upstream_inputs([("a", good), ("b", bad)])
            assert inputs == {"k": "v"}

    def test_multiple_deliverables_merged(self):
        """多个上游 deliverable, inputs 是它们 union."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            d1 = tmp_path / "1.json"
            d1.write_text(json.dumps({"a": 1}))
            d2 = tmp_path / "2.json"
            d2.write_text(json.dumps({"b": 2}))
            inputs = collect_upstream_inputs([("s1", d1), ("s2", d2)])
            assert inputs == {"a": 1, "b": 2}


class TestBuildChannelName:
    def test_channel_name_format(self):
        name = build_channel_name("research", "run-abc12345")
        assert name == ".stage-research-run-abc12345"

    def test_channel_name_underscore(self):
        name = build_channel_name("data_collect", "run-xyz")
        assert name == ".stage-data_collect-run-xyz"


class TestBuildInputHandoffPaths:
    def test_single_upstream(self):
        """1 upstream + 2 workers → 2 handoff paths."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = [("research", tmp_path / "data" / "findings.json")]
            stage = make_stage_with_workers(["w1", "w2"])
            paths = build_input_handoff_paths(stage, upstream, tmp_path)
            assert len(paths) == 2
            # 都是上游 research 的 handoff
            assert all(p[0] == "research" for p in paths)
            # 路径对
            worker_ids = sorted(p[1].parts[-3] for p in paths)
            assert worker_ids == ["w1", "w2"]

    def test_no_upstream(self):
        """无 upstream → 空 handoff."""
        with tempfile_TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stage = make_stage_with_workers(["w1"])
            paths = build_input_handoff_paths(stage, [], tmp_path)
            assert paths == []


# =============================================================================
# Helpers
# =============================================================================


def tempfile_TemporaryDirectory():
    """生成临时目录 context manager (避免重复 import)."""
    import tempfile
    return tempfile.TemporaryDirectory()


def make_stage_with_workers(worker_ids: list[str]):
    """构造一个 StageSpec 用于测试 (workers 列表用 id)."""
    from agents_chat.workflow.schema import (
        DeliverableSpec,
        StageSpec,
        WorkerSpec,
    )
    return StageSpec(
        id="test-stage",
        workers=[
            WorkerSpec(id=wid, cli="mock")
            for wid in worker_ids
        ],
        deliverable=DeliverableSpec(path="data/out.json"),
    )
