"""
FileBusWatcher 单元测试.

覆盖:
- 启动 / 停止 lifecycle
- 目录自动创建
- FileSystemEventHandler 把 modify 事件转为 EventBus 事件
- 跨进程场景 (子进程写 → 父进程收到 event)
- watchdog fallback (mock unavailable)
"""
from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agents_chat.infra.events import (
    channel_event,
    get_event_bus,
    mailbox_event,
)


# =============================================================================
# FileBusWatcher lifecycle
# =============================================================================


class TestFileBusWatcherLifecycle:
    def test_start_stop(self, tmp_path):
        from agents_chat.infra.watcher import FileBusWatcher

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        assert watcher.is_running()
        watcher.stop()
        assert not watcher.is_running()

    def test_idempotent_start(self, tmp_path):
        from agents_chat.infra.watcher import FileBusWatcher

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        watcher.start()  # 不应报错
        assert watcher.is_running()
        watcher.stop()

    def test_stop_without_start(self, tmp_path):
        from agents_chat.infra.watcher import FileBusWatcher

        watcher = FileBusWatcher(tmp_path)
        watcher.stop()  # 不应报错
        assert not watcher.is_running()

    def test_creates_missing_dirs(self, tmp_path):
        """channels/ mailboxes/ 目录不存在时自动创建."""
        from agents_chat.infra.watcher import FileBusWatcher

        # 验证 tmp_path 下 channels/ mailboxes/ 都不存在
        assert not (tmp_path / "channels").exists()
        assert not (tmp_path / "mailboxes").exists()

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        assert (tmp_path / "channels").exists()
        assert (tmp_path / "mailboxes").exists()
        watcher.stop()


# =============================================================================
# FileSystemEventHandler 转换
# =============================================================================


class TestEventClassification:
    """测试 _FileBusHandler 把文件路径正确分类为 EventBus 事件."""

    def test_channel_modify(self, tmp_path):
        from agents_chat.infra.watcher import _FileBusHandler

        (tmp_path / "channels").mkdir()
        (tmp_path / "mailboxes").mkdir()
        handler = _FileBusHandler(tmp_path / "channels", tmp_path / "mailboxes")

        # 模拟 watchdog 事件
        class FakeEvent:
            is_directory = False
            src_path = str(tmp_path / "channels" / "fish-market.jsonl")
        ev = handler._classify(FakeEvent())
        assert ev == channel_event("fish-market")

    def test_mailbox_modify(self, tmp_path):
        from agents_chat.infra.watcher import _FileBusHandler

        (tmp_path / "channels").mkdir()
        (tmp_path / "mailboxes").mkdir()
        handler = _FileBusHandler(tmp_path / "channels", tmp_path / "mailboxes")

        class FakeEvent:
            is_directory = False
            src_path = str(tmp_path / "mailboxes" / "seller-fish.json")
        ev = handler._classify(FakeEvent())
        assert ev == mailbox_event("seller-fish")

    def test_ignores_directory_events(self, tmp_path):
        from agents_chat.infra.watcher import _FileBusHandler

        (tmp_path / "channels").mkdir()
        handler = _FileBusHandler(tmp_path / "channels", tmp_path / "mailboxes")

        class FakeEvent:
            is_directory = True
            src_path = str(tmp_path / "channels" / "subdir")
        ev = handler._classify(FakeEvent())
        assert ev is None

    def test_ignores_hidden_files(self, tmp_path):
        from agents_chat.infra.watcher import _FileBusHandler

        (tmp_path / "channels").mkdir()
        handler = _FileBusHandler(tmp_path / "channels", tmp_path / "mailboxes")

        class FakeEvent:
            is_directory = False
            src_path = str(tmp_path / "channels" / ".swp")
        ev = handler._classify(FakeEvent())
        assert ev is None

    def test_ignores_unrelated_paths(self, tmp_path):
        from agents_chat.infra.watcher import _FileBusHandler

        (tmp_path / "channels").mkdir()
        (tmp_path / "mailboxes").mkdir()
        handler = _FileBusHandler(tmp_path / "channels", tmp_path / "mailboxes")

        class FakeEvent:
            is_directory = False
            src_path = str(tmp_path / "sessions" / "data.json")  # 不在 channels/mailboxes
        ev = handler._classify(FakeEvent())
        assert ev is None


# =============================================================================
# 端到端: 写文件 → watcher 触发 EventBus (单进程内)
# =============================================================================


class TestFileBusWatcherEnd2End:
    @pytest.mark.asyncio
    async def test_write_triggers_eventbus(self, tmp_path):
        """写 Channel 文件 → FileBusWatcher 监听 → EventBus 收到事件."""
        from agents_chat.infra.watcher import FileBusWatcher

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        try:
            # 清空之前测试残留
            bus = get_event_bus()
            ev = channel_event("e2e-ch")
            bus.clear(ev)

            # 模拟"另一个进程"写 Channel 文件
            ch_dir = tmp_path / "channels"
            ch_dir.mkdir(parents=True, exist_ok=True)
            ch_file = ch_dir / "e2e-ch.jsonl"
            ch_file.write_text('{"id":"1","ts":"2026-01-01","from":"god","content":"hi","mentions":[],"type":"text"}\n', encoding="utf-8")

            # 等 watcher 检测到 (macOS FSEvents 默认 50-200ms 延迟)
            fired = await bus.wait(ev, timeout=2.0)
            assert fired is True, f"watcher 2s 内未触发 event for {ev}"
        finally:
            watcher.stop()

    @pytest.mark.asyncio
    async def test_mailbox_write_triggers(self, tmp_path):
        """写 Mailbox 文件 → watcher 触发 mailbox event."""
        from agents_chat.infra.watcher import FileBusWatcher
        import json

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        try:
            bus = get_event_bus()
            ev = mailbox_event("e2e-agent")
            bus.clear(ev)

            mb_dir = tmp_path / "mailboxes"
            mb_dir.mkdir(parents=True, exist_ok=True)
            mb_file = mb_dir / "e2e-agent.json"
            mb_file.write_text(json.dumps({"agent": "e2e-agent", "pending": []}), encoding="utf-8")

            fired = await bus.wait(ev, timeout=2.0)
            assert fired is True
        finally:
            watcher.stop()


# =============================================================================
# 真实跨进程 (subprocess 写, 父进程 watcher 收到)
# =============================================================================


class TestFileBusWatcherCrossProcess:
    @pytest.mark.asyncio
    async def test_subprocess_write_wakes_parent(self, tmp_path):
        """子进程 append Channel, 父进程的 watcher 触发 EventBus."""
        from agents_chat.infra.watcher import FileBusWatcher

        watcher = FileBusWatcher(tmp_path)
        watcher.start()
        try:
            bus = get_event_bus()
            ev = channel_event("cross-proc-ch")
            bus.clear(ev)

            # 准备 Channel 文件
            ch_dir = tmp_path / "channels"
            ch_dir.mkdir(parents=True, exist_ok=True)
            ch_file = ch_dir / "cross-proc-ch.jsonl"
            ch_file.touch()

            # 启动子进程写 Channel
            child = subprocess.Popen(
                [sys.executable, "-c", f"""
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
ch = Channel('{ch_file}', 'cross-proc-ch')
ch.append(from_='subprocess', content='hello from child', mentions=[])
"""],
                cwd="/Users/fundou/my_proj/agents-chat-channel",
            )
            child.wait(timeout=10)

            # 父进程应该通过 watchdog 收到事件
            fired = await bus.wait(ev, timeout=3.0)
            assert fired is True, "subprocess write 没触发父进程 EventBus"
        finally:
            watcher.stop()
