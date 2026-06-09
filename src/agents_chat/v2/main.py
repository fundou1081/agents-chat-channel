"""
Backward-compat shim: 实际实现在 infra.main.

保留 `python -m agents_chat.v2.main` 和 `from agents_chat.v2.main import ...` 兼容.
"""
from .infra.main import *  # noqa: F401,F403
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
