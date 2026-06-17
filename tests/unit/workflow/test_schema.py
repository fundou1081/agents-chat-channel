"""
Schema tests — dedicated Pydantic model validator tests.

Previously covered by test_loader indirectly; now standalone.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError
from agents_chat.workflow.schema import (
    WorkerSpec, DeliverableSpec, StageSpec, WorkflowSpec,
)


class TestWorkerSpec:
    def test_valid(self):
        w = WorkerSpec(id="my-worker", cli="mock")
        assert w.id == "my-worker"
        assert w.cli == "mock"
        assert w.model == ""

    def test_defaults(self):
        w = WorkerSpec(id="w1")
        assert w.cli == "opencode"
        assert w.model == ""
        assert w.system_prompt == ""

    def test_invalid_id_number_start(self):
        with pytest.raises(ValidationError, match="worker id"):
            WorkerSpec(id="123abc", cli="mock")

    def test_invalid_id_special_char(self):
        with pytest.raises(ValidationError, match="worker id"):
            WorkerSpec(id="my@worker", cli="mock")

    def test_invalid_cli(self):
        with pytest.raises(ValidationError, match="literal_error"):
            WorkerSpec(id="w", cli="unknown")

    def test_model_can_be_set(self):
        w = WorkerSpec(id="w", cli="qwen", model="qwen/qwen-max")
        assert w.model == "qwen/qwen-max"

    def test_system_prompt_can_be_set(self):
        w = WorkerSpec(id="w", system_prompt="you are helper")
        assert w.system_prompt == "you are helper"

    def test_id_max_length(self):
        long_id = "a" * 64
        w = WorkerSpec(id=long_id)
        assert w.id == long_id
        with pytest.raises(ValidationError):
            WorkerSpec(id="a" * 65)


class TestDeliverableSpec:
    def test_path_only(self):
        d = DeliverableSpec(path="out/a.json")
        assert d.path == "out/a.json"
        assert d.paths is None

    def test_paths_only(self):
        d = DeliverableSpec(paths=["out/a.json", "out/b.json"])
        assert d.paths == ["out/a.json", "out/b.json"]

    def test_dir_only(self):
        d = DeliverableSpec(dir="out/")
        assert d.dir == "out/"

    def test_must_specify_one(self):
        with pytest.raises(ValidationError, match="must specify one"):
            DeliverableSpec()

    def test_exclusive_path_and_paths(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            DeliverableSpec(path="a.json", paths=["b.json"])

    def test_exclusive_paths_and_dir(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            DeliverableSpec(paths=["a.json"], dir="out/")

    def test_empty_paths_list_fails(self):
        with pytest.raises(ValidationError, match="must specify one"):
            DeliverableSpec(paths=[])

    def test_formats_length_must_match_paths(self):
        with pytest.raises(ValidationError, match="must have same length"):
            DeliverableSpec(paths=["a.json", "b.json"], formats=["json"])

    def test_json_schema_alias_from_yaml(self):
        d = DeliverableSpec.model_validate({
            "path": "out/a.json",
            "schema": {"type": "object"},
        })
        assert d.json_schema == {"type": "object"}

    def test_json_schema_direct(self):
        d = DeliverableSpec(path="out/a.json", json_schema={"type": "object"})
        assert d.json_schema == {"type": "object"}

    def test_no_userwarning(self):
        import warnings
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            DeliverableSpec(path="out/a.json", json_schema={"type": "object"})
            schema_warnings = [x for x in w if "shadows" in str(x.message)]
            assert len(schema_warnings) == 0

    def test_get_all_paths(self):
        d = DeliverableSpec(paths=["out/a.json", "out/b.json"])
        assert d.get_all_paths() == ["out/a.json", "out/b.json"]
        d = DeliverableSpec(path="out/x.json")
        assert d.get_all_paths() == ["out/x.json"]

    def test_get_primary_path(self):
        d = DeliverableSpec(paths=["out/a.json", "out/b.json"])
        assert d.get_primary_path() == "out/a.json"


class TestStageSpec:
    def test_valid_minimal(self):
        s = StageSpec(
            id="research", workers=[WorkerSpec(id="w")],
            deliverable=DeliverableSpec(path="out/a.json"),
        )
        assert s.id == "research"
        assert s.timeout == 600
        assert s.depends_on == []

    def test_duplicate_worker_ids(self):
        with pytest.raises(ValidationError, match="duplicate worker ids"):
            StageSpec(
                id="s",
                workers=[WorkerSpec(id="w1"), WorkerSpec(id="w1")],
                deliverable=DeliverableSpec(path="out/a.json"),
            )

    def test_self_dependency(self):
        with pytest.raises(ValidationError, match="cannot depend on itself"):
            StageSpec(
                id="s", depends_on=["s"],
                workers=[WorkerSpec(id="w")],
                deliverable=DeliverableSpec(path="out/a.json"),
            )

    def test_stage_id_number_start(self):
        with pytest.raises(ValidationError, match="stage id"):
            StageSpec(
                id="123stage", workers=[WorkerSpec(id="w")],
                deliverable=DeliverableSpec(path="out/a.json"),
            )

    def test_upper_case_id_allowed(self):
        s = StageSpec(
            id="MyStage", workers=[WorkerSpec(id="w")],
            deliverable=DeliverableSpec(path="out/a.json"),
        )
        assert s.id == "MyStage"

    def test_empty_workers_fails(self):
        with pytest.raises(ValidationError):
            StageSpec(
                id="s", workers=[],
                deliverable=DeliverableSpec(path="out/a.json"),
            )


class TestWorkflowSpec:
    def test_valid(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", workers=[WorkerSpec(id="w")],
                      deliverable=DeliverableSpec(path="out/a.json")),
        ])
        assert wf.name == "test"
        assert len(wf.stages) == 1

    def test_duplicate_stage_ids(self):
        with pytest.raises(ValidationError, match="duplicate stage ids"):
            WorkflowSpec(name="test", stages=[
                StageSpec(id="a", workers=[WorkerSpec(id="w1")],
                          deliverable=DeliverableSpec(path="out/a.json")),
                StageSpec(id="a", workers=[WorkerSpec(id="w2")],
                          deliverable=DeliverableSpec(path="out/b.json")),
            ])

    def test_empty_stages_fails(self):
        with pytest.raises(ValidationError):
            WorkflowSpec(name="test", stages=[])

    def test_topological_linear(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", workers=[WorkerSpec(id="wa")],
                      deliverable=DeliverableSpec(path="out/a.json")),
            StageSpec(id="b", depends_on=["a"], workers=[WorkerSpec(id="wb")],
                      deliverable=DeliverableSpec(path="out/b.json")),
            StageSpec(id="c", depends_on=["b"], workers=[WorkerSpec(id="wc")],
                      deliverable=DeliverableSpec(path="out/c.json")),
        ])
        order = wf.topological_order()
        assert [s.id for s in order] == ["a", "b", "c"]

    def test_topological_diamond(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", workers=[WorkerSpec(id="wa")],
                      deliverable=DeliverableSpec(path="out/a.json")),
            StageSpec(id="b", depends_on=["a"], workers=[WorkerSpec(id="wb")],
                      deliverable=DeliverableSpec(path="out/b.json")),
            StageSpec(id="c", depends_on=["a"], workers=[WorkerSpec(id="wc")],
                      deliverable=DeliverableSpec(path="out/c.json")),
            StageSpec(id="d", depends_on=["b","c"], workers=[WorkerSpec(id="wd")],
                      deliverable=DeliverableSpec(path="out/d.json")),
        ])
        order = wf.topological_order()
        assert order[0].id == "a"
        assert order[-1].id == "d"
        mids = {s.id for s in order[1:3]}
        assert mids == {"b", "c"}

    def test_topological_cycle(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", depends_on=["b"], workers=[WorkerSpec(id="wa")],
                      deliverable=DeliverableSpec(path="out/a.json")),
            StageSpec(id="b", depends_on=["a"], workers=[WorkerSpec(id="wb")],
                      deliverable=DeliverableSpec(path="out/b.json")),
        ])
        with pytest.raises(ValueError, match="cycle detected"):
            wf.topological_order()

    def test_topological_unknown_dep(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", depends_on=["nonexistent"],
                      workers=[WorkerSpec(id="wa")],
                      deliverable=DeliverableSpec(path="out/a.json")),
        ])
        with pytest.raises(ValueError, match="unknown stage"):
            wf.topological_order()

    def test_get_stage(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", workers=[WorkerSpec(id="w")],
                      deliverable=DeliverableSpec(path="out/a.json")),
        ])
        assert wf.get_stage("a") is not None
        assert wf.get_stage("nonexistent") is None

    def test_downstream_stages(self):
        wf = WorkflowSpec(name="test", stages=[
            StageSpec(id="a", workers=[WorkerSpec(id="wa")],
                      deliverable=DeliverableSpec(path="out/a.json")),
            StageSpec(id="b", depends_on=["a"], workers=[WorkerSpec(id="wb")],
                      deliverable=DeliverableSpec(path="out/b.json")),
            StageSpec(id="c", depends_on=["a"], workers=[WorkerSpec(id="wc")],
                      deliverable=DeliverableSpec(path="out/c.json")),
        ])
        ds = wf.downstream_stages("a")
        assert {s.id for s in ds} == {"b", "c"}
        assert wf.downstream_stages("c") == []

    def test_workflow_name_invalid(self):
        with pytest.raises(ValidationError, match="workflow name"):
            WorkflowSpec(name="123", stages=[
                StageSpec(id="a", workers=[WorkerSpec(id="w")],
                          deliverable=DeliverableSpec(path="out/a.json")),
            ])
