"""
agents-chat-channel v2.0 CLI 入口 (顶层).

提供 `python -m agents_chat.main` 入口和子命令 (init/run-worker/post/...).

子命令实现在 `agents_chat.infra.main`.
"""
from .infra.main import (  # noqa: F401
    main,
    cmd_init,
    cmd_run_worker,
    cmd_post,
    cmd_status,
    cmd_tail,
    cmd_inbox,
    cmd_reset,
)

__all__ = [
    "main",
    "cmd_init",
    "cmd_run_worker",
    "cmd_post",
    "cmd_status",
    "cmd_tail",
    "cmd_inbox",
    "cmd_reset",
]


if __name__ == "__main__":
    main()
