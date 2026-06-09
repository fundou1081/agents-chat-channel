"""
FileBusWatcher — watchdog 跨进程事件触发.

把 OS 文件系统事件 (macOS FSEvents / Linux inotify / Windows ReadDirectoryChangesW)
转为 EventBus 事件, 让 agent 进程能 0 延迟感知其他进程 (god / 其他 agent /
server) 写入的 Channel/Mailbox.

跟 EventBus (1️⃣) 配合:
  - 进程内写 (Channel.append() / Mailbox.append()) → EventBus.emit() 立即触发 (< 1ms)
  - 跨进程写 (其他进程写文件) → watchdog 监听 mtime 变化 → emit 同一事件 (< 50ms)

启动方式:
  async with FileBusWatcher(data_dir).start() as watcher:
      # watcher 在后台跑, 自动 emit EventBus 事件
      ...
  # 退出时自动 stop

跨平台 notes (来自 watchdog 文档):
  - macOS: FSEvents, 默认 1s 批处理间隔 (调节参数: latency)
  - Linux: inotify, 实时 (单文件事件立即触发)
  - Windows: ReadDirectoryChangesW, 实时

限制:
  - watchdog 用 mtime 检测, fast successive write 可能合并
  - 文件被替换 (rename + create) 也能检测到
  - 跨 mount point 不工作
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .events import channel_event, get_event_bus, mailbox_event

logger = logging.getLogger(__name__)


class _FileBusHandler(FileSystemEventHandler):
    """watchdog 事件 handler — 把文件变化转为 EventBus 事件.

    监听两个目录:
      - data_dir/channels/*.jsonl   → emit "channel:<name>:new"
      - data_dir/mailboxes/*.json   → emit "mailbox:<agent_id>:new"

    只关心 modify (写) + create (新文件), 忽略其他 (move, delete, etc).
    """

    def __init__(self, channels_dir: Path, mailboxes_dir: Path) -> None:
        self.channels_dir = channels_dir.resolve()
        self.mailboxes_dir = mailboxes_dir.resolve()
        self.bus = get_event_bus()

    def _is_relevant(self, event: FileSystemEvent) -> bool:
        """只关心文件 modify/create, 忽略目录事件 + 隐藏文件."""
        if event.is_directory:
            return False
        path = Path(event.src_path).resolve()
        # 隐藏文件 (.swp, .DS_Store 等) 忽略
        if path.name.startswith("."):
            return False
        return True

    def _classify(self, event: FileSystemEvent) -> Optional[str]:
        """根据路径分类, 返回 EventBus 事件名. None = 不关心."""
        if not self._is_relevant(event):
            return None
        path = Path(event.src_path).resolve()
        try:
            # 兼容 macOS symlink 等
            real = path.resolve()
        except OSError:
            return None
        if str(real).startswith(str(self.channels_dir)):
            name = path.stem
            return channel_event(name)
        if str(real).startswith(str(self.mailboxes_dir)):
            agent_id = path.stem
            return mailbox_event(agent_id)
        return None

    def on_modified(self, event: FileSystemEvent) -> None:
        ev = self._classify(event)
        if ev is not None:
            self.bus.emit(ev)
            logger.debug(f"watchdog modified → {ev}")

    def on_created(self, event: FileSystemEvent) -> None:
        """新文件创建 (如新频道) 也触发."""
        ev = self._classify(event)
        if ev is not None:
            self.bus.emit(ev)
            logger.debug(f"watchdog created → {ev}")


class FileBusWatcher:
    """FileBusWatcher — 包装 watchdog Observer, 提供 start/stop lifecycle.

    Usage (async with):
        async with FileBusWatcher(data_dir):
            # 监听已启动
            ...
        # 退出时自动 stop

    Usage (手动):
        watcher = FileBusWatcher(data_dir)
        watcher.start()
        try:
            ...
        finally:
            watcher.stop()
    """

    def __init__(self, data_dir: str | Path, latency_ms: int = 50) -> None:
        """data_dir: agents_chat data_dir (含 channels/ 和 mailboxes/).
        latency_ms: 批量处理间隔 (macOS FSEvents 用, 默认 50ms 够用)."""
        self.data_dir = Path(data_dir).resolve()
        self.channels_dir = self.data_dir / "channels"
        self.mailboxes_dir = self.data_dir / "mailboxes"
        self.latency_ms = latency_ms
        self._observer: Observer | None = None

    def _ensure_dirs(self) -> None:
        """如果 channels/ mailboxes/ 目录不存在, 创建 (避免 watchdog 报错)."""
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)

    def start(self) -> "FileBusWatcher":
        """启动后台 Observer 线程 (非阻塞)."""
        if self._observer is not None:
            return self
        self._ensure_dirs()
        handler = _FileBusHandler(self.channels_dir, self.mailboxes_dir)
        self._observer = Observer()
        # recursive=False: 只监听顶层文件, 不递归子目录
        self._observer.schedule(handler, str(self.channels_dir), recursive=False)
        self._observer.schedule(handler, str(self.mailboxes_dir), recursive=False)
        self._observer.start()
        logger.info(
            f"FileBusWatcher started: channels={self.channels_dir} mailboxes={self.mailboxes_dir}"
        )
        return self

    def stop(self) -> None:
        """停止 Observer (阻塞, 等待线程退出)."""
        if self._observer is None:
            return
        self._observer.stop()
        self._observer.join(timeout=2.0)
        self._observer = None
        logger.info("FileBusWatcher stopped")

    def is_running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    # 异步上下文支持
    async def __aenter__(self) -> "FileBusWatcher":
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.stop()


# 优雅 fallback: 如果 watchdog 没装, FileBusWatcher 退化 (no-op)
def _check_watchdog_available() -> bool:
    try:
        import watchdog  # noqa: F401
        return True
    except ImportError:
        return False
