"""Test OpenCodeAgent integration. Requires opencode CLI installed."""
import json
import shutil
import pytest

from agents_chat.llm.opencode import OpenCodeAgent
from agents_chat.models import Persona, TickContext, Mail


@pytest.fixture
def skip_if_no_opencode():
    if not shutil.which("opencode"):
        pytest.skip("opencode not installed")


@pytest.mark.asyncio
async def test_opencode_simple_echo(tmp_path, skip_if_no_opencode):
    """opencode 能跑简单任务 + 输出 JSON。"""
    agent = OpenCodeAgent(model="opencode/minimax-m3-free", timeout_seconds=60)
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "test.txt").write_text("hello")

    p = Persona(
        id="test-agent", display_name="测试", title="tester",
        system_prompt="你是一个测试 agent。", workdir=str(workdir),
    )
    ctx = TickContext(persona=p, new_mail=[], active_sessions=[])

    # 简单任务: list files + 输出 JSON
    decision = await agent.think(
        system=p.system_prompt,
        user="用 bash 跑 ls -la, 然后输出 JSON: {\"thinking\": \"<what you did>\", \"outgoing_mail\": [], \"closed_sessions\": [], \"next_status\": \"idle\"}",
        ctx=ctx,
    )
    assert decision is not None
    print(f"Decision: thinking={decision.thinking[:100]}")
    print(f"Actions: {len(decision.actions)}")
    # 至少应该有一些 tool calls (bash 跑 ls)
    # 我们的 agent 会把 opencode 的 tool_use 转成 actions


@pytest.mark.asyncio
async def test_opencode_create_file(tmp_path, skip_if_no_opencode):
    """opencode 真的能改文件。"""
    agent = OpenCodeAgent(model="opencode/minimax-m3-free", timeout_seconds=90)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    p = Persona(
        id="zhang", display_name="小张", title="前端",
        system_prompt="你是小张, 前端工程师。", workdir=str(workdir),
    )
    ctx = TickContext(persona=p, new_mail=[], active_sessions=[])

    # 让 opencode 写一个文件
    decision = await agent.think(
        system=p.system_prompt,
        user="在当前目录创建 hello.py, 内容是 def hello(): return 'hi from opencode'。完成后输出 JSON: {\"thinking\": \"done\", \"outgoing_mail\": [], \"closed_sessions\": [], \"next_status\": \"working\"}",
        ctx=ctx,
    )
    assert decision is not None
    # 文件应该被创建
    hello_file = workdir / "hello.py"
    assert hello_file.exists(), f"hello.py not created. Decision: {decision.thinking}"
    content = hello_file.read_text()
    assert "def hello" in content
    assert "hi from opencode" in content
