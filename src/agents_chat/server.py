"""
agents-chat-channel v2.0 FastAPI server (顶层).

提供 `python -m agents_chat.server` 入口 (--port 8765 --data-dir ./data_v2).

应用实现在 `agents_chat.infra.server`.
"""
from .infra.server import (  # noqa: F401
    main,
    create_app,
)

__all__ = ["main", "create_app"]


if __name__ == "__main__":
    main()
