"""Unit tests for v2.0 CLI (base + mock + qwen)."""
import pytest

from agents_chat.v2.cli.base import CLIResponse, new_session_id
from agents_chat.v2.cli.mock import MockCLI


class TestCLIResponse:
    def test_ok(self):
        r = CLIResponse(output_text="hi")
        assert r.ok
        assert r.error == ""

    def test_error(self):
        r = CLIResponse(output_text="", error="boom")
        assert not r.ok
        assert r.error == "boom"


class TestNewSessionId:
    def test_unique(self):
        ids = {new_session_id() for _ in range(100)}
        assert len(ids) == 100

    def test_prefix(self):
        sid = new_session_id("qwen")
        assert sid.startswith("qwen_")


class TestMockCLI:
    @pytest.mark.asyncio
    async def test_first_invoke_creates_session(self):
        cli = MockCLI()
        r = await cli.invoke("hello world")
        assert r.ok
        assert r.new_session_id is not None
        assert r.new_session_id.startswith("mock_")
        assert "hello world" in r.output_text
        assert "task_" in r.output_text  # 提取了 task_id
        # 包含 STATUS 块
        assert "<!--STATUS" in r.output_text
        assert "progress: 100" in r.output_text

    @pytest.mark.asyncio
    async def test_resume_reuses_session(self):
        cli = MockCLI()
        r1 = await cli.invoke("first task")
        sid = r1.new_session_id
        r2 = await cli.invoke("continue", resume_session=sid)
        assert r2.new_session_id is None  # resume 不返回 new
        assert "continue" in r2.output_text

    @pytest.mark.asyncio
    async def test_extract_task_id_from_prompt(self):
        cli = MockCLI()
        r = await cli.invoke("process task_abc123 now")
        assert "task_abc123" in r.output_text

    @pytest.mark.asyncio
    async def test_call_count(self):
        cli = MockCLI()
        await cli.invoke("x")
        await cli.invoke("y")
        await cli.invoke("z", resume_session="mock_1")
        assert cli.call_count == 3

    @pytest.mark.asyncio
    async def test_chinese_prompt(self):
        cli = MockCLI()
        r = await cli.invoke("处理 task_数据库 的连接泄漏")
        assert r.ok
        assert "task_数据库" in r.output_text
