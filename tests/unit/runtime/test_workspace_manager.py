"""
Tests for WorkspaceManager.
"""
import pytest
import sys, os, tempfile, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", "src"))

from agents_chat.infra.worker_factory import WorkspaceManager


class TestWorkspaceManager:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ws_path = os.path.join(self.tmpdir, "seller-fish")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_init_creates_subdirs(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="卖鱼小贩", system_prompt="你是卖鱼小贩", cli_name="opencode")
        assert os.path.isdir(os.path.join(self.ws_path, "skills"))
        assert os.path.isdir(os.path.join(self.ws_path, "mcp"))
        assert os.path.isdir(os.path.join(self.ws_path, "instructions"))

    def test_roles_md(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="卖鱼小贩", system_prompt="你是seller-fish, 讨价还价", cli_name="opencode")
        content = open(os.path.join(self.ws_path, "roles.md")).read()
        assert "seller-fish" in content or "卖鱼小贩" in content

    def test_opencode_md_created(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="卖鱼小贩", cli_name="opencode")
        assert os.path.exists(os.path.join(self.ws_path, "opencode.md"))

    def test_config_json(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="卖鱼小贩", cli_name="opencode", skills=["bargaining"], mcp_servers=["fish-api"])
        cfg = json.load(open(os.path.join(self.ws_path, "config.json")))
        assert cfg["role"] == "卖鱼小贩"
        assert cfg["cli"] == "opencode"
        assert "bargaining" in cfg["skills"]
        assert "fish-api" in cfg["mcp_servers"]

    def test_mcp_stub_json(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="卖鱼小贩", cli_name="opencode")
        wm._setup_mcp(["fish-api"])
        mcp_path = os.path.join(self.ws_path, "mcp", "fish-api.json")
        assert os.path.exists(mcp_path), f"Expected {mcp_path} to exist"
        # 注意: stub JSON 含 // 注释, 用 text 模式读而非 json.load
        content = open(mcp_path).read()
        assert "fish-api" in content
        assert "mcp_server" in content

    def test_add_instruction(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(cli_name="opencode")
        wm.add_instruction("pricing.md", "# 定价策略\n根据季节调整价格")
        content = open(os.path.join(self.ws_path, "instructions", "pricing.md")).read()
        assert "定价策略" in content

    def test_list_skills_empty(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(cli_name="opencode")
        assert wm.list_skills() == []

    def test_read_roles(self):
        wm = WorkspaceManager(self.ws_path)
        wm.init(role="小贩", system_prompt="我是鱼贩", cli_name="opencode")
        roles = wm.read_roles()
        assert "鱼贩" in roles

    def test_existing_roles_merged(self):
        """If roles.md exists, _init_workspace merges (preserve user edits + append prompt)."""
        os.makedirs(self.ws_path)
        existing = "# 自定义角色\n我是自定义角色定义"
        open(os.path.join(self.ws_path, "roles.md"), "w").write(existing)
        from agents_chat.infra.worker_factory import _init_workspace
        from pathlib import Path
        _init_workspace(
            workspace_dir=Path(self.ws_path),
            cli_name="opencode",
            role="小贩",
            system_prompt="系统提示",
            skills=None,
            mcp_servers=None,
            role_template="",
        )
        content = open(os.path.join(self.ws_path, "roles.md")).read()
        # _init_workspace merges: existing + new prompt
        assert "自定义角色" in content  # existing preserved
        assert "系统提示" in content     # new prompt appended