"""
busd — agents-chat-channel 内存事件总线守护进程.

跟 server 进程同生命周期 (server 启 → busd 启, server 关 → busd 关).

机制:
  - UDS (Unix Domain Socket) server, listen 在 data_dir/busd.sock
  - 接受 client 连接, 收事件, 广播给所有 client
  - 协议: newline-delimited JSON `{"event": "<name>", "ts": <float>}\n`
  - 多 client 并发, 每 client 1 thread
  - 写 socket path 到 data_dir/busd.sock.path (给 client 找)

降级:
  - 端口冲突 (sock 文件已存在) → 检测 + unlink
  - 客户端断线 → 安静忽略
  - server 关闭 → stdin EOF, busd 退出

用法:
  # 单独启 (不推荐, 应由 server 启)
  python -m agents_chat.infra.busd --data-dir ./data_v2

  # 由 server lifespan 启
  lifespan spawn (subprocess.Popen)

延迟: 0.01-1ms (本地 UDS, 单跳)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import selectors
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("busd")

DEFAULT_SOCK_NAME = "busd.sock"
DEFAULT_SOCK_PATH_FILE = "busd.sock.path"  # 写这个文件给 client 找


class BusDaemon:
    """UDS-based event bus daemon.

    接受多个 client, 每个 client 1 thread (recv), 1 thread (send).
    收事件 → 广播到所有 client (除发事件的).
    """

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._selector = selectors.DefaultSelector()
        self._server_sock: Optional[socket.socket] = None
        self._clients: dict[socket.socket, tuple[bytes, list[bytes]]] = {}  # sock -> (client_id, send_buffer)
        self._lock = threading.Lock()
        self._stopped = False

    def _setup_server(self) -> None:
        """清理旧 sock 文件 + bind + listen."""
        # 清理旧 socket (上次崩溃可能留下)
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_sock.bind(str(self.socket_path))
        self._server_sock.listen(16)
        # 非阻塞 + 注册到 selector
        self._server_sock.setblocking(False)
        self._selector.register(self._server_sock, selectors.EVENT_READ, data=None)

    def _accept(self) -> None:
        """接受新 client 连接 (selector event)."""
        try:
            conn, _ = self._server_sock.accept()
        except (BlockingIOError, OSError):
            return
        conn.setblocking(True)  # 收/发用阻塞 IO (thread 里)
        with self._lock:
            self._clients[conn] = (b"client", [])
        # 启 recv/send thread
        threading.Thread(target=self._client_recv_loop, args=(conn,), daemon=True).start()
        logger.info(f"busd: new client, total={len(self._clients)}")

    def _client_recv_loop(self, conn: socket.socket) -> None:
        """每 client 1 thread: 收事件 → 广播."""
        try:
            buf = b""
            while not self._stopped:
                chunk = conn.recv(4096)
                if not chunk:
                    break  # EOF
                buf += chunk
                # 按 newline 切事件
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        self._broadcast(line + b"\n", sender=conn)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        finally:
            self._remove_client(conn)

    def _broadcast(self, payload: bytes, sender: socket.socket) -> None:
        """广播事件给所有 client (除 sender)."""
        with self._lock:
            dead = []
            for sock, (cid, _) in self._clients.items():
                if sock is sender:
                    continue
                try:
                    sock.sendall(payload)
                except (BrokenPipeError, OSError):
                    dead.append(sock)
            for sock in dead:
                self._remove_client(sock)
        logger.debug(f"busd: broadcast {len(payload)}B to {len(self._clients) - 1} clients")

    def _remove_client(self, conn: socket.socket) -> None:
        with self._lock:
            self._clients.pop(conn, None)
        try:
            conn.close()
        except OSError:
            pass
        logger.info(f"busd: client removed, remaining={len(self._clients)}")

    def serve_forever(self) -> None:
        """主循环: accept + 处理 selector 事件."""
        self._setup_server()
        logger.info(f"busd: listening on {self.socket_path} (pid={os.getpid()})")
        try:
            while not self._stopped:
                events = self._selector.select(timeout=1.0)
                for key, _ in events:
                    if key.fileobj is self._server_sock:
                        self._accept()
        except KeyboardInterrupt:
            pass
        finally:
            self._cleanup()

    def _cleanup(self) -> None:
        logger.info("busd: cleaning up...")
        self._stopped = True
        with self._lock:
            clients = list(self._clients.keys())
        for c in clients:
            self._remove_client(c)
        try:
            self._selector.unregister(self._server_sock)
        except (KeyError, ValueError):
            pass
        try:
            self._server_sock.close()
        except OSError:
            pass
        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass
        logger.info("busd: stopped")


def write_socket_path_file(data_dir: Path, socket_path: Path) -> None:
    """写 socket path 文件, 给 client 找."""
    path_file = data_dir / DEFAULT_SOCK_PATH_FILE
    path_file.write_text(str(socket_path), encoding="utf-8")


def read_socket_path_file(data_dir: Path) -> Optional[Path]:
    """读 socket path 文件, 返回 Path 或 None (busd 没启).

    注意: 只检查 path file 存在, 不检查 socket 本身.
    如果 socket 还没创建/被刚删, client 会连接失败然后重试.
    """
    path_file = data_dir / DEFAULT_SOCK_PATH_FILE
    if not path_file.exists():
        return None
    try:
        p = Path(path_file.read_text(encoding="utf-8").strip())
        return p
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="agents-chat-channel busd")
    parser.add_argument("--data-dir", default="./data_v2", help="agents_chat data_dir")
    parser.add_argument("--socket", default=None, help="UDS path (默认 $data_dir/busd.sock)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(message)s",
    )

    data_dir = Path(args.data_dir).resolve()
    sock_path = Path(args.socket) if args.socket else (data_dir / DEFAULT_SOCK_NAME)

    # 写 path 文件给 client
    write_socket_path_file(data_dir, sock_path)

    # 优雅退出
    def handle_signal(signum, frame):
        logger.info(f"busd: received signal {signum}, exiting...")
        sys.exit(0)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    daemon = BusDaemon(sock_path)
    daemon.serve_forever()


if __name__ == "__main__":
    main()
