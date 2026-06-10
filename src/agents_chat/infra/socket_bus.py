"""
SocketBusClient — 连 busd 的 UDS client.

特性:
- 自动读 busd.sock.path 找 socket
- 自动重连 (busd 重启时)
- 后台 recv thread, 收到事件 → emit 到 EventBus
- send 同步 (一行 newline-delimited JSON)
- busd 不在时 send 安静失败 (降级到 watchdog + poll)

跟 EventBus + FileBusWatcher 配合:
  进程内:   Channel.append() → EventBus.emit() (< 1μs)
  跨进程:   FileBusWatcher (watchdog) → EventBus.emit() (< 50ms)
  busd:     SocketBusClient.send() → busd → 其他 client → EventBus.emit() (< 1ms)
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Optional

from .busd import read_socket_path_file
from .events import get_event_bus

logger = logging.getLogger("socket-bus")


class SocketBusClient:
    """busd client — 自动连接 + 重连 + 后台 recv.

    Usage:
        client = SocketBusClient(data_dir)  # 自动找 socket
        client.start()  # 启 recv thread
        ...
        client.emit("channel:foo:new")  # 同步发
        ...
        client.stop()  # 停
    """

    def __init__(
        self,
        data_dir: str | Path,
        reconnect_interval: float = 1.0,
        recv_buffer_size: int = 4096,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.reconnect_interval = reconnect_interval
        self.recv_buffer_size = recv_buffer_size
        self._sock: Optional[socket.socket] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._connect_thread: Optional[threading.Thread] = None
        self._stopped = False
        self._send_lock = threading.Lock()
        self._connected = threading.Event()

    def _find_socket(self) -> Optional[Path]:
        """从 data_dir/busd.sock.path 找 socket 路径."""
        return read_socket_path_file(self.data_dir)

    def start(self) -> None:
        """启动后台 connect + recv thread. 不会抛错 (busd 不在时安静等待)."""
        if self._recv_thread is not None:
            return
        self._stopped = False
        # 用 daemon thread, 进程退出自动结束
        self._connect_thread = threading.Thread(
            target=self._connect_loop, daemon=True, name="socket-bus-connect"
        )
        self._connect_thread.start()

    def stop(self) -> None:
        self._stopped = True
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._connected.clear()

    def _connect_loop(self) -> None:
        """主循环: 尝试连接, 启动 recv, 断线重连."""
        while not self._stopped:
            sock_path = self._find_socket()
            if sock_path is None:
                # busd 没启, 等待
                time.sleep(self.reconnect_interval)
                continue
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                sock.connect(str(sock_path))
                sock.settimeout(None)  # 阻塞 recv
                self._sock = sock
                self._connected.set()
                logger.info(f"socket-bus: connected to {sock_path}")
                # 启 recv thread
                self._recv_thread = threading.Thread(
                    target=self._recv_loop, args=(sock,), daemon=True, name="socket-bus-recv"
                )
                self._recv_thread.start()
                # 等 recv thread 退出 (断线)
                self._recv_thread.join()
                self._connected.clear()
                self._sock = None
                logger.info("socket-bus: disconnected, will reconnect...")
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                logger.debug(f"socket-bus: connect failed: {e}")
                time.sleep(self.reconnect_interval)
            except Exception as e:
                logger.warning(f"socket-bus: unexpected error: {e}")
                time.sleep(self.reconnect_interval)

    def _recv_loop(self, sock: socket.socket) -> None:
        """收事件 → emit 到 EventBus."""
        buf = b""
        bus = get_event_bus()
        while not self._stopped:
            try:
                chunk = sock.recv(self.recv_buffer_size)
                if not chunk:
                    break  # EOF, 触发重连
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                        event_name = msg.get("event")
                        if event_name:
                            bus.emit(event_name)
                            logger.debug(f"socket-bus: received {event_name}")
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"socket-bus: bad message: {e}")
            except (ConnectionResetError, OSError):
                break

    def emit(self, event: str, wait_ms: int = 500) -> bool:
        """同步发事件给 busd. 返回 True = 成功, False = 失败 (busd 不在).

        如果还没连上, 最多等 wait_ms 毫秒 (默认 500ms).
        这样子进程刚启时 Channel.append() 调用不会丢失事件.

        失败时不抛错, 安静降级 (watchdog + poll 兜底).
        """
        deadline = time.monotonic() + wait_ms / 1000.0
        while time.monotonic() < deadline:
            if self._connected.is_set() and self._sock is not None:
                try:
                    msg = json.dumps({"event": event, "ts": time.time()}) + "\n"
                    with self._send_lock:
                        self._sock.sendall(msg.encode("utf-8"))
                    return True
                except (BrokenPipeError, OSError):
                    return False
            time.sleep(0.05)  # 50ms 重试
        return False

    def is_connected(self) -> bool:
        return self._connected.is_set()


# 进程级单例 (按 data_dir 缓存)
_clients: dict[str, SocketBusClient] = {}
_clients_lock = threading.Lock()


def get_socket_bus_client(data_dir: str | Path) -> SocketBusClient:
    """获取进程级 SocketBusClient 单例 (按 data_dir 缓存)."""
    key = str(Path(data_dir).resolve())
    client = _clients.get(key)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(key)
        if client is not None:
            return client
        client = SocketBusClient(data_dir)
        client.start()
        _clients[key] = client
    return client


def emit_to_bus(data_dir: str | Path, event: str) -> bool:
    """便捷 API: 发事件给 busd. 失败时返 False (不报错, 兑底)."""
    try:
        return get_socket_bus_client(data_dir).emit(event)
    except Exception:
        return False
