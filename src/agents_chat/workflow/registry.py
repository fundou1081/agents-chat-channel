"""
Workflow Registry — 全局 active scheduler 跟踪.

用于 cancel endpoint 查找 run_id 对应的 scheduler 并取消.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .scheduler import WorkflowScheduler

logger = logging.getLogger("workflow-registry")


class WorkflowRegistry:
    """线程安全的 active scheduler 注册表.

    Usage:
        registry = WorkflowRegistry()
        scheduler = WorkflowScheduler(...)
        registry.register(scheduler)
        # ... 后台跑 scheduler.run()
        # 取消时:
        scheduler = registry.get(run_id)
        if scheduler:
            scheduler.cancel()
    """
    _instance: Optional["WorkflowRegistry"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self._schedulers: dict[str, WorkflowScheduler] = {}
        self._lock = threading.Lock()

    @classmethod
    def get_default(cls) -> "WorkflowRegistry":
        """单例: 全局共享一个 registry."""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = WorkflowRegistry()
        return cls._instance

    def register(self, scheduler: WorkflowScheduler) -> None:
        with self._lock:
            self._schedulers[scheduler.run_id] = scheduler
            logger.debug(f"registered scheduler {scheduler.run_id}")

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._schedulers.pop(run_id, None)
            logger.debug(f"unregistered scheduler {run_id}")

    def get(self, run_id: str) -> Optional[WorkflowScheduler]:
        with self._lock:
            return self._schedulers.get(run_id)

    def list_active(self) -> list[str]:
        """返所有 active run_id."""
        with self._lock:
            return list(self._schedulers.keys())
