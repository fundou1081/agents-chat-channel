"""
v2 Files: 文件总线 I/O 抽象.

| Module | 职责 |
|--------|------|
| channel  | 频道消息 (jsonl append-only) |
| mailbox  | Agent 邮箱 (jsonl per-agent) |
| lock     | 文件锁 (O_CREAT | O_EXCL 原子创建 + TTL 续约) |
"""
from .channel import Channel, fuzzy_resolve_mention
from .lock import (
    DEFAULT_TTL_SECONDS,
    acquire,
    force_release,
    force_release_if_expired,
    is_expired,
    is_held_by,
    read_lock_info,
    refresh,
    release,
)
from .mailbox import Mailbox

__all__ = [
    "Channel",
    "Mailbox",
    "fuzzy_resolve_mention",
    # lock API
    "DEFAULT_TTL_SECONDS",
    "acquire",
    "release",
    "force_release",
    "force_release_if_expired",
    "refresh",
    "is_expired",
    "is_held_by",
    "read_lock_info",
]
