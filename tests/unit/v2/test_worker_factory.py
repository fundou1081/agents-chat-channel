"""
Tests for WorkerFactory + CLI registry.
"""
import pytest
from pathlib import Path
import sys, os, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", "src"))

from agents_chat.v2.worker_factory import (
    WorkerFactory,
    register_cli,
    list_clis,
    get_cli_class,
)
from agents_chat.v2.cli.base import CLI


class DummyCLI:
    """测试用 CLI."""
    name = "dummy"

    async def execute(self, prompt, session_id=None, workspace_dir=None):
        from agents_chat.v2.cli.base import CLIResponse
        return CLIResponse(output_text="dummy", new_session_id="s_dummy", raw="dummy")


class TestCLIRegistry:
    def test_list_clis_default(self):
        assert "opencode" in list_clis()
        assert "qwen" in list_clis()
        assert "mock" in list_clis()

    def test_register_and_get(self):
        register_cli("dummy", DummyCLI)
        assert "dummy" in list_clis()
        assert get_cli_class("dummy") is DummyCLI

    def test_get_unknown(self):
        assert get_cli_class("nonexistent") is None


class TestWorkerFactoryCreate:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "data"
        self.data_dir.mkdir()

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_mock_worker(self):
        worker = WorkerFactory.create(
            agent_id="test-agent",
            cli_type="mock",
            data_dir=self.data_dir,
            mode="passive",
        )
        assert worker.agent_id == "test-agent"
        assert worker.cli.name == "mock"
        assert worker.mode == "passive"

    def test_create_opencode_worker(self):
        worker = WorkerFactory.create(
            agent_id="opencode-agent",
            cli_type="opencode",
            data_dir=self.data_dir,
            mode="proactive",
            subscriptions=["general"],
            system_prompt="你是卖鱼小贩",
        )
        assert worker.agent_id == "opencode-agent"
        assert worker.cli.name == "opencode"
        assert worker.subscriptions == {"general"}

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown CLI type"):
            WorkerFactory.create(
                agent_id="bad-agent",
                cli_type="nonexistent",
                data_dir=self.data_dir,
            )

    def test_create_with_decision_config(self):
        worker = WorkerFactory.create(
            agent_id="dm-agent",
            cli_type="mock",
            data_dir=self.data_dir,
            decision_config={"api_key": "sk-test", "model": "gpt-4"},
        )
        # decision_config 被传给 Agent, Agent 内部创建 DecisionMaker
        assert worker.decision_maker is not None

    def test_create_all(self):
        workers = WorkerFactory.create_all(
            {
                "seller": {"cli_type": "mock", "subscriptions": ["fish"]},
                "buyer": {"cli_type": "mock", "subscriptions": ["fish"]},
            },
            data_dir=self.data_dir,
            mode="proactive",
        )
        assert len(workers) == 2
        assert workers["seller"].agent_id == "seller"
        assert workers["buyer"].agent_id == "buyer"
        assert workers["seller"].mode == "proactive"