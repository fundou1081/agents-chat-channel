"""
Backward-compat shim: 实际实现在 infra.server.

保留 `python -m agents_chat.v2.server` 兼容.
"""
from .infra.server import *  # noqa: F401,F403
from .infra.server import (  # noqa: F401
    main,
    create_app,
)

__all__ = ["main", "create_app"]


if __name__ == "__main__":
    main()
