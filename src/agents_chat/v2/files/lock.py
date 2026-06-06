"""
File-based lock for v2.0 task claim mechanism.

设计: O_CREAT | O_EXCL 原子创建, mtime 判定 TTL.
- acquire(): 原子创建锁文件, 内容为 owner_id + ISO ts
- release(): 验证 owner 后删除
- refresh(): touch 文件 (更新 mtime, 续约)
- is_expired(): 看 mtime 是否超过 ttl
- force_release_if_expired(): 过期则删除

锁文件路径: locks/task_{task_id}.lock
锁文件内容: {owner_id}|{iso_timestamp}
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Default TTL: 5 min (符合 v2.0 设计文档)
DEFAULT_TTL_SECONDS = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def acquire(
    lock_path: str | Path,
    owner_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """原子获取锁. 返回 True=成功, False=已被占用.

    内容: {"owner": owner_id, "acquired_at": iso, "ttl": ttl}
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "owner": owner_id,
        "acquired_at": _now_iso(),
        "ttl": ttl_seconds,
    })
    try:
        # O_CREAT | O_EXCL | O_WRONLY — 原子. 文件已存在则抛 FileExistsError
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        try:
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except FileExistsError:
        return False


def release(lock_path: str | Path, owner_id: str) -> bool:
    """释放锁 (验证 owner 匹配). 不匹配则不删, 返回 False."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        return False
    info = read_lock_info(lock_path)
    if info and info.get("owner") == owner_id:
        lock_path.unlink(missing_ok=True)
        return True
    return False


def force_release(lock_path: str | Path) -> bool:
    """强制释放锁 (不验证 owner). 用于 Scanner 清理过期锁."""
    lock_path = Path(lock_path)
    if lock_path.exists():
        lock_path.unlink()
        return True
    return False


def refresh(lock_path: str | Path, owner_id: str) -> bool:
    """续约 (touch mtime). 用于 Agent 写 STATUS 时."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        return False
    info = read_lock_info(lock_path)
    if not info or info.get("owner") != owner_id:
        return False
    # 更新 mtime 到 now
    os.utime(str(lock_path), (_now_ts(), _now_ts()))
    return True


def is_expired(lock_path: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
    """锁是否过期 (mtime 超过 ttl)."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        return True  # 不存在的锁 = 过期
    mtime = lock_path.stat().st_mtime
    return (_now_ts() - mtime) > ttl_seconds


def force_release_if_expired(
    lock_path: str | Path, ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """如果过期, 强制删除. 返回 True 表示删了."""
    if is_expired(lock_path, ttl_seconds):
        return force_release(lock_path)
    return False


def read_lock_info(lock_path: str | Path) -> Optional[dict]:
    """读锁文件内容. 不存在返回 None."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        return None
    try:
        return json.loads(lock_path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def is_held_by(lock_path: str | Path, owner_id: str) -> bool:
    """锁是否被指定 owner 持有 (未过期)."""
    if is_expired(lock_path):
        return False
    info = read_lock_info(lock_path)
    return info is not None and info.get("owner") == owner_id


@contextmanager
def lock(lock_path: str | Path, owner_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS):
    """Context manager: with lock(path, "agent_a"): ... 自动 release."""
    acquired = acquire(lock_path, owner_id, ttl_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            release(lock_path, owner_id)
