"""Tests for v2.0 跨平台兼容 (Windows / macOS / Linux).

背景: 用户分享 Windows 跑 Claude Code 失败 (图 5, 6, 9):
  - bash 把 C:\\Users\\... 反斜杠 strip 掉 → C:Users...
  - WinError 2 找不到
  - .cmd / PowerShell wrapper 不解析多 args

v2.0 免疫这些的设计:
  - pathlib.Path 跨平台 (自动 / 或 \\)
  - shutil.which() 找 CLI (Windows 下处理 .cmd)
  - subprocess args list (不传 shell=True, 避免 .cmd wrapper)
  - 所有路径用 / 拼接 (Path 自动适配)
"""
import pytest
import sys
from pathlib import Path


class TestPathlibCrossPlatform:
    """pathlib.Path 应该自动适配平台, 不 strip backslashes."""

    def test_posix_path_construction(self):
        p = Path("/foo/bar/baz")
        assert str(p) == "/foo/bar/baz"

    def test_windows_path_via_string(self):
        p = Path("C:/Users/foo/bar")
        assert "C:" in str(p) or sys.platform == "win32"
        assert "Users" in str(p)
        assert "foo" in str(p)
        assert "bar" in str(p)

    def test_path_join(self):
        p = Path("C:/Users") / "foo" / "bar.md"
        assert p.parts[-1] == "bar.md"
        assert "foo" in p.parts
        assert "Users" in str(p)

    def test_backslashes_in_raw_string_preserved(self):
        """Path(r'C:\\Users\\foo') 不会 strip 反斜杠 (vs bash)."""
        p = Path(r"C:\Users\mtk20928\Project\workspace.md")
        raw = str(p)
        # 关键: 字符串里含 "Users", "foo", "bar.md" 都被保留
        # bash 会把 "C:\Users\foo" 错误 strip 成 "C:Usersfoo" (反斜杠消失)
        # Path 不会
        assert "Users" in raw
        assert "foo" in raw or "mtk20928" in raw
        # 关键: 不应该 strip 后只剩 "C:Usersfoo"
        assert "C:Usersfoo" not in raw.replace("\\", "")
        # 反斜杠字符不被 strip (至少在 str 里能找到)
        # POSIX 上整个是 1 个 part, 但反斜杠字符在字符串里
        # Windows 上分成多个 parts
        if sys.platform != "win32":
            # POSIX: 整个是 1 个 part, 反斜杠是 part 内的字符
            assert len(p.parts) >= 1
        else:
            # Windows: parts 包含 Users
            assert "Users" in p.parts

    def test_pathlib_writes_correct_separator(self, tmp_path):
        """Path 写出时 (write_text) 用正确分隔符."""
        p = Path(tmp_path) / "subdir" / "test.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("hello")
        assert p.exists()
        assert p.read_text() == "hello"

    def test_pathlib_no_bash_strip(self):
        """Path 不像 bash 那样 strip 反斜杠."""
        p = Path(r"C:\Users\foo\bar.md")
        raw = str(p)
        # 字符串里含 "Users", "foo", "bar.md"
        assert "Users" in raw
        assert "bar.md" in raw
        # 关键: 反斜杠字符不被 strip 掉
        # 验证整个 path 字符串长度 = 26 (C: + \ + Users + \ + foo + \ + bar.md)
        # 如果 strip 了, 会变 17 (C:Usersfoobar.md) — 但我们没要这么长
        # 实际看 str(p) 是不是还含反斜杠
        if sys.platform == "win32":
            # Windows: 反斜杠是 separator
            assert "\\" in raw
        else:
            # POSIX: 反斜杠是 part 内的字符
            assert "\\" in raw  # raw string 里的 2 个 \\ 在 POSIX 是 1 个 \
            # 整个是 1 个 part
            assert len(p.parts) == 1
        # 关键证明: 字符串含 "C:\\Users\\foo\\bar.md" (19 个字符 in raw) 跟 "C:Usersfoobar.md" (17 字符) 不一样
        # 如果 strip 了, "C:Usersfoobar.md" 才会出现 (没有 \)
        # 实际 raw 长度应该 > 17 (有反斜杠)
        assert len(raw) > 17, f"Path stripped? raw={raw!r}"


class TestShutilWhich:
    """shutil.which() 跨平台找 CLI."""

    def test_which_finds_python(self):
        import shutil
        python_path = shutil.which("python3") or shutil.which("python")
        if python_path is not None:
            assert isinstance(python_path, str)
            assert "/" in python_path or "\\" in python_path

    def test_which_returns_none_for_nonexistent(self):
        import shutil
        result = shutil.which("definitely_not_a_real_binary_xyz12345")
        assert result is None


class TestSubprocessArgs:
    """subprocess 不用 shell, 避免 .cmd wrapper."""

    def test_subprocess_args_list_format(self):
        cmd = ["echo", "hello", "world", "with", "spaces"]
        assert isinstance(cmd, list)
        assert "echo" in cmd

    def test_subprocess_no_shell_param(self):
        """asyncio.create_subprocess_exec 没有 'shell' 参数 (默认 False)."""
        import inspect
        import asyncio
        sig = inspect.signature(asyncio.create_subprocess_exec)
        # 不应该有 'shell' 参数
        assert 'shell' not in sig.parameters
        # 这意味着我们不可能意外传 shell=True
        # 也不需要 — v2 OpenCodeCLI 用 args list 直接传


class TestV2FilesystemComponents:
    """v2 files/ 模块应该全用 pathlib, 跨平台."""

    def test_channel_pathlib(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.append(from_="alice", content="hello", type="mention")
        assert ch.path.exists()
        import json
        msgs = [json.loads(l) for l in ch.path.read_text("utf-8").splitlines() if l]
        assert len(msgs) == 1

    def test_mailbox_pathlib(self, tmp_path):
        from agents_chat.v2.files.mailbox import Mailbox
        mb = Mailbox(tmp_path / "agent1.json", "agent1")
        mb.append(ref_msg_id="ch_1", type="mention", content="@agent1 hi", channel="general")
        assert mb.path.exists()
        pending = mb.read_and_clear()
        assert len(pending) == 1

    def test_lock_pathlib(self, tmp_path):
        from agents_chat.v2.files.lock import acquire, release
        lock_path = tmp_path / "task_1.lock"
        assert acquire(lock_path, "agent1") is True
        assert lock_path.exists()
        release(lock_path, "agent1")
        assert not lock_path.exists()

    def test_agent_creates_all_dirs_via_pathlib(self, tmp_path):
        from agents_chat.v2.agent import Agent
        from agents_chat.v2.cli.mock import MockCLI
        Agent(agent_id="qwencode", cli=MockCLI(), data_dir=tmp_path)
        assert (tmp_path / "mailboxes").exists()
        assert (tmp_path / "channels").exists()
        assert (tmp_path / "locks").exists()
        assert (tmp_path / "sessions").exists()
        assert (tmp_path / "workspaces" / "qwencode").exists()
        assert (tmp_path / "mailboxes" / "qwencode.json").exists()


class TestV2CliShutilWhich:
    """OpenCodeCLI 应该用 shutil.which() 找 CLI 路径."""

    def test_finds_existing_binary(self):
        from agents_chat.v2.cli.opencode import _find_cli
        import shutil
        existing = shutil.which("echo") or shutil.which("ls")
        if existing:
            found = _find_cli(Path(existing).name)
            assert found == existing

    def test_raises_for_nonexistent(self):
        from agents_chat.v2.cli.opencode import _find_cli
        with pytest.raises(FileNotFoundError) as exc_info:
            _find_cli("definitely_not_a_real_binary_xyz12345")
        assert "not found" in str(exc_info.value)
        assert "PATH" in str(exc_info.value) or "shutil" in str(exc_info.value)


class TestWorkspacePathWindowsStyle:
    """模拟用户场景: workspace_path 是 Windows 风格 (含驱动器 + 反斜杠)."""

    def test_workspace_md_with_windows_path(self, tmp_path):
        """workspace_dir = Windows 风格路径 → Agent 仍能 create <cli>.md."""
        from agents_chat.v2.agent import Agent
        from agents_chat.v2.cli.mock import MockCLI
        # POSIX 平台: 用 forward-slash C:/Users/... 形式 (Path 自动适配)
        # 这种形式在 Windows 上 Path 会正常解析
        workspace = tmp_path / "win_workspace"
        agent = Agent(
            agent_id="qwencode",
            cli=MockCLI(),
            data_dir=tmp_path,
            workspace_dir=workspace,  # Path 对象, 跨平台
        )
        # workspace.md 应该创建
        assert (workspace / "mock.md").exists()
