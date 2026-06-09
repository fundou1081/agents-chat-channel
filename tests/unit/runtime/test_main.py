"""Tests for v2.0 main CLI (subprocess smoke tests)."""
import subprocess
import sys
from pathlib import Path

import pytest


def run_cli(*args, data_dir: str) -> subprocess.CompletedProcess:
    """Helper: run main.py via subprocess."""
    return subprocess.run(
        [sys.executable, "-m", "agents_chat.main", "--data-dir", data_dir, *args],
        capture_output=True, text=True, cwd=str(Path(__file__).parent.parent.parent.parent),
    )


class TestMainInit:
    def test_init_creates_layout(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        r = run_cli("init", data_dir=data_dir)
        assert r.returncode == 0
        d = Path(data_dir)
        assert (d / "channels").exists()
        assert (d / "mailboxes").exists()
        assert (d / "sessions").exists()
        assert (d / "locks").exists()
        assert (d / "channels" / "general.jsonl").exists()
        assert (d / "state_board.json").exists()

    def test_init_idempotent(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("init", data_dir=data_dir)
        assert r.returncode == 0


class TestMainPost:
    def test_post_creates_message(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("post", "general", "@qwencode 写个 hello.py", "--from", "god", data_dir=data_dir)
        assert r.returncode == 0
        # 频道应该有 1 条
        r2 = run_cli("tail", "general", "--n", "5", data_dir=data_dir)
        assert "@qwencode" in r2.stdout
        assert "hello.py" in r2.stdout

    def test_post_detects_task(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("post", "general", "[TASK task_xyz] 干点啥", data_dir=data_dir)
        assert "task_broadcast" in r.stdout


class TestMainStatus:
    def test_empty_status(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("status", data_dir=data_dir)
        assert r.returncode == 0
        assert "(empty)" in r.stdout


class TestMainTailInbox:
    def test_tail_empty(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("tail", "general", data_dir=data_dir)
        assert "empty" in r.stdout

    def test_inbox_empty(self, tmp_path):
        data_dir = str(tmp_path / "v2_data")
        run_cli("init", data_dir=data_dir)
        r = run_cli("inbox", "qwencode", data_dir=data_dir)
        assert "empty" in r.stdout
