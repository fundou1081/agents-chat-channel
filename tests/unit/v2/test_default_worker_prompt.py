"""
Tests for DEFAULT_WORKER_PROMPT_TEMPLATE (Worker 基础 prompt).
"""
import pytest
import sys, os, tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", "src"))

from agents_chat.v2.infra.worker_factory import (
    DEFAULT_WORKER_PROMPT_TEMPLATE,
    _init_workspace,
)


class TestDefaultWorkerPrompt:
    def test_template_has_5_rules(self):
        """默认模板必须包含 5 条通信规则."""
        assert "频道通信规则" in DEFAULT_WORKER_PROMPT_TEMPLATE
        assert "@名字" in DEFAULT_WORKER_PROMPT_TEMPLATE
        assert "@频道管理员" in DEFAULT_WORKER_PROMPT_TEMPLATE
        assert "[STATUS]" in DEFAULT_WORKER_PROMPT_TEMPLATE
        assert "收信人" in DEFAULT_WORKER_PROMPT_TEMPLATE

    def test_template_format(self):
        out = DEFAULT_WORKER_PROMPT_TEMPLATE.format(
            agent_name="小红",
            role="创意设计师",
            example_agent="阿明",
        )
        assert "你是小红。创意设计师。" in out
        assert "@阿明" in out

    def test_init_workspace_uses_default(self):
        """无 system_prompt / role_template 时使用默认模板."""
        tmpdir = tempfile.mkdtemp()
        ws = Path(tmpdir) / "worker-x"
        _init_workspace(
            workspace_dir=ws,
            cli_name="opencode",
            role="测试角色",
            system_prompt="",
            skills=None,
            mcp_servers=None,
            role_template="",
        )
        content = open(ws / "roles.md").read()
        assert "频道通信规则" in content
        assert "你是worker-x" in content
        assert "测试角色" in content
        import shutil; shutil.rmtree(tmpdir)

    def test_init_workspace_with_role_template(self):
        """有 role_template 时覆盖默认模板."""
        tmpdir = tempfile.mkdtemp()
        ws = Path(tmpdir) / "worker-y"
        _init_workspace(
            workspace_dir=ws,
            cli_name="opencode",
            role="鱼贩",
            system_prompt="",
            skills=None,
            mcp_servers=None,
            role_template="你是{role}, 卖鱼小贩. 策略: 开价 100.",
        )
        content = open(ws / "roles.md").read()
        # role_template 覆盖默认模板
        assert "卖鱼小贩" in content
        assert "开价 100" in content
        import shutil; shutil.rmtree(tmpdir)

    def test_init_workspace_custom_system_prompt(self):
        """显式 system_prompt 覆盖默认."""
        tmpdir = tempfile.mkdtemp()
        ws = Path(tmpdir) / "worker-z"
        _init_workspace(
            workspace_dir=ws,
            cli_name="opencode",
            role="鱼贩",
            system_prompt="特殊指令: 不要说话",
            skills=None,
            mcp_servers=None,
            role_template="",
        )
        content = open(ws / "roles.md").read()
        assert "特殊指令" in content
        import shutil; shutil.rmtree(tmpdir)

    def test_priority_order(self):
        """优先级: system_prompt > role_template > 默认模板."""
        tmpdir = tempfile.mkdtemp()
        ws = Path(tmpdir) / "worker-w"
        _init_workspace(
            workspace_dir=ws,
            cli_name="opencode",
            role="鱼贩",
            system_prompt="SYSTEM_PROMPT_VAL",
            skills=None,
            mcp_servers=None,
            role_template="ROLE_TEMPLATE_VAL",
        )
        content = open(ws / "roles.md").read()
        # system_prompt 优先
        assert "SYSTEM_PROMPT_VAL" in content
        assert "ROLE_TEMPLATE_VAL" not in content
        import shutil; shutil.rmtree(tmpdir)
