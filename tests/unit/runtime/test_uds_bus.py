"""
busd + SocketBusClient 测试.

覆盖:
- busd 启停 lifecycle
- busd 接受 client, 广播事件
- SocketBusClient 自动连 + 收事件 + emit 到 EventBus
- 跨进程真实: subprocess 跑 busd, 父进程 client 收到
- 跟 server lifespan 集成 (server 启 → busd 启, server 关 → busd 关)
- 降级: busd 不在时 client 安静等待
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from agents_chat.infra.events import (
    channel_event,
    get_event_bus,
    mailbox_event,
)
from agents_chat.infra.busd import (
    BusDaemon,
    DEFAULT_SOCK_NAME,
    DEFAULT_SOCK_PATH_FILE,
    read_socket_path_file,
    write_socket_path_file,
)


# =============================================================================
# busd 基本 lifecycle
# =============================================================================


class TestBusdLifecycle:
    def test_setup_and_cleanup(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        daemon = BusDaemon(sock_path)
        daemon._setup_server()
        assert sock_path.exists()
        daemon._cleanup()
        assert not sock_path.exists()

    def test_setup_replaces_stale_socket(self, tmp_path):
        sock_path = tmp_path / "test.sock"
        # 预先 touch 一个旧 socket 文件
        sock_path.touch()
        daemon = BusDaemon(sock_path)
        daemon._setup_server()  # 应该清理掉旧的
        # 现在能连上
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock_path))
        s.close()
        daemon._cleanup()


class TestSocketPathFile:
    def test_write_and_read(self, tmp_path):
        sock_path = tmp_path / "x.sock"
        write_socket_path_file(tmp_path, sock_path)
        assert read_socket_path_file(tmp_path) == sock_path

    def test_read_missing(self, tmp_path):
        assert read_socket_path_file(tmp_path) is None

    def test_read_stale(self, tmp_path):
        # path 文件存在但 socket 不存在 — 返 path (client 试连会失败, 然后重试)
        (tmp_path / DEFAULT_SOCK_PATH_FILE).write_text(
            str(tmp_path / "nonexistent.sock")
        )
        result = read_socket_path_file(tmp_path)
        assert result == tmp_path / "nonexistent.sock"


# =============================================================================
# busd 接受 client + 广播
# =============================================================================


class TestBusdBroadcast:
    def test_single_client_send_recv(self, tmp_path):
        sock_path = tmp_path / "bus.sock"
        daemon = BusDaemon(sock_path)
        daemon._setup_server()
        try:
            # 启 busd 主循环在后台 thread
            t = __import__("threading").Thread(target=daemon.serve_forever, daemon=True)
            t.start()
            time.sleep(0.1)

            # client 1 连, 发, 收
            c1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c1.connect(str(sock_path))
            c1.settimeout(2.0)
            # 发事件
            c1.sendall(b'{"event":"test:hello"}\n')
            # client 不应该收到自己发的 (broadcast 跳过 sender)
            # 验证: 在 200ms 内 recv 应该 timeout (没事件)
            c1.settimeout(0.2)
            with pytest.raises(socket.timeout):
                c1.recv(1024)
            c1.close()
        finally:
            daemon._cleanup()

    def test_two_clients_broadcast(self, tmp_path):
        sock_path = tmp_path / "bus.sock"
        daemon = BusDaemon(sock_path)
        daemon._setup_server()
        try:
            t = __import__("threading").Thread(target=daemon.serve_forever, daemon=True)
            t.start()
            time.sleep(0.2)  # 让 busd 完全启

            c1 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c1.connect(str(sock_path))
            c1.settimeout(2.0)
            c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c2.connect(str(sock_path))
            c2.settimeout(2.0)
            time.sleep(0.1)  # 让 busd 注册 c1 + c2

            # c1 发, c2 应该收到
            c1.sendall(b'{"event":"test:broadcast"}\n')
            time.sleep(0.1)  # 让 busd 内部线程完成 recv + broadcast
            received = c2.recv(1024)
            assert b"test:broadcast" in received

            c1.close()
            c2.close()
        finally:
            daemon._cleanup()


# =============================================================================
# SocketBusClient lifecycle
# =============================================================================


class TestSocketBusClientLifecycle:
    def test_connect_to_busd(self, tmp_path):
        from agents_chat.infra.socket_bus import SocketBusClient

        # 启 busd
        sock_path = tmp_path / "bus.sock"
        daemon = BusDaemon(sock_path)
        daemon._setup_server()
        write_socket_path_file(tmp_path, sock_path)
        t = __import__("threading").Thread(target=daemon.serve_forever, daemon=True)
        t.start()
        time.sleep(0.1)

        try:
            client = SocketBusClient(tmp_path)
            client.start()
            # 等连上
            for _ in range(20):
                if client.is_connected():
                    break
                time.sleep(0.1)
            assert client.is_connected()

            # 测延迟: emit 同步返 + 收事件 (从另一 client 模拟)
            from agents_chat.infra.events import get_event_bus
            bus = get_event_bus()
            ev = channel_event("test-ch")
            bus.clear(ev)

            # emit
            ok = client.emit(channel_event("test-ch"))
            assert ok is True

            # 由于 client 既发又收 (loopback 跳过 sender), 我们需要外部 client 来 broadcast
            # 改: 启第 2 client 验证它能收到
            c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c2.connect(str(sock_path))
            c2.settimeout(2.0)
            # c2 发, 我们的 client 收
            c2.sendall(b'{"event":"channel:test-ch:new"}\n')
            # 等 client 收到 + emit 到 bus
            async def wait_for_event():
                return await bus.wait(channel_event("test-ch"), timeout=1.0)
            fired = asyncio.run(wait_for_event())
            assert fired is True
            c2.close()
        finally:
            client.stop()
            daemon._cleanup()

    def test_busd_not_running_silent(self, tmp_path):
        """busd 不在时, client 启动后安静等待, 不抛错."""
        from agents_chat.infra.socket_bus import SocketBusClient

        # 不启 busd, 写个 path 指向不存在的 socket
        sock_path = tmp_path / "nonexistent.sock"
        write_socket_path_file(tmp_path, sock_path)

        client = SocketBusClient(tmp_path, reconnect_interval=0.1)
        client.start()
        time.sleep(0.3)
        # 不应连上
        assert not client.is_connected()
        # emit 返 False
        assert client.emit("test:event") is False
        client.stop()

    def test_emit_to_bus_helper(self, tmp_path):
        from agents_chat.infra.socket_bus import emit_to_bus

        # busd 不在, emit 返 False, 不报错
        write_socket_path_file(tmp_path, tmp_path / "nonexistent.sock")
        result = emit_to_bus(str(tmp_path), "test:event")
        assert result is False


# =============================================================================
# 真实跨进程: subprocess 跑 busd, 父进程 client 收到
# =============================================================================


class TestBusdCrossProcess:
    @pytest.mark.asyncio
    async def test_subprocess_busd_parent_client(self, tmp_path):
        """子进程跑 busd, 父进程启 SocketBusClient, 验证能收到事件."""
        from agents_chat.infra.socket_bus import SocketBusClient

        # 启 busd 作为子进程
        proc = subprocess.Popen(
            [sys.executable, "-m", "agents_chat.infra.busd",
             "--data-dir", str(tmp_path),
             "--socket", str(tmp_path / DEFAULT_SOCK_NAME)],
            cwd="/Users/fundou/my_proj/agents-chat-channel",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        try:
            # 等 busd 写 path 文件
            for _ in range(30):
                if (tmp_path / DEFAULT_SOCK_PATH_FILE).exists():
                    break
                await asyncio.sleep(0.1)

            # 父进程 client
            client = SocketBusClient(tmp_path, reconnect_interval=0.2)
            client.start()
            for _ in range(30):
                if client.is_connected():
                    break
                await asyncio.sleep(0.1)
            assert client.is_connected()

            # 用 raw socket 发事件, 让 client 收
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(str(tmp_path / DEFAULT_SOCK_NAME))
            s.sendall(b'{"event":"channel:cross-proc:new"}\n')
            s.close()

            # 等 client 收 + emit 到 bus
            bus = get_event_bus()
            ev = channel_event("cross-proc")
            bus.clear(ev)
            fired = await bus.wait(ev, timeout=2.0)
            assert fired is True

            client.stop()
        finally:
            proc.terminate()
            proc.wait(timeout=2)

    @pytest.mark.asyncio
    async def test_subprocess_client_writes_channel(self, tmp_path):
        """子进程用 Channel.append() 写, 父进程通过 busd 收事件."""
        from agents_chat.infra.socket_bus import SocketBusClient

        # 启 busd
        proc = subprocess.Popen(
            [sys.executable, "-m", "agents_chat.infra.busd",
             "--data-dir", str(tmp_path)],
            cwd="/Users/fundou/my_proj/agents-chat-channel",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(30):
                if (tmp_path / DEFAULT_SOCK_PATH_FILE).exists():
                    break
                await asyncio.sleep(0.1)

            # 父进程连
            client = SocketBusClient(tmp_path, reconnect_interval=0.2)
            client.start()
            for _ in range(30):
                if client.is_connected():
                    break
                await asyncio.sleep(0.1)
            bus = get_event_bus()
            ev = channel_event("sub-ch")
            bus.clear(ev)

            # 子进程 append Channel
            (tmp_path / "channels").mkdir(parents=True, exist_ok=True)
            child = subprocess.Popen(
                [sys.executable, "-c", f"""
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
ch = Channel('{tmp_path}/channels/sub-ch.jsonl', 'sub-ch')
ch.append(from_='child', content='hi from child', mentions=[])
"""],
                cwd="/Users/fundou/my_proj/agents-chat-channel",
            )
            child.wait(timeout=5)

            # 父进程应通过 busd 收到 (busd 优先于 watchdog, 应该 0-50ms)
            fired = await bus.wait(ev, timeout=2.0)
            assert fired is True

            client.stop()
        finally:
            proc.terminate()
            proc.wait(timeout=2)


# =============================================================================
# 跟 server lifespan 集成 (端到端)
# =============================================================================


class TestServerLifespanIntegration:
    def test_server_spawns_and_cleans_busd(self, tmp_path):
        """直接验证: server lifespan 应该启 busd, 退出时关 busd."""
        # 跳过 uvicorn, 直接调 create_app + 用 TestClient 模拟 lifespan
        from fastapi.testclient import TestClient
        from agents_chat.infra.server import create_app

        app = create_app(data_dir=tmp_path, host="127.0.0.1", port=0)
        with TestClient(app) as client:
            # 进入 lifespan: busd 应该 spawn
            assert (tmp_path / DEFAULT_SOCK_PATH_FILE).exists()
            sock_path = read_socket_path_file(tmp_path)
            assert sock_path is not None
            assert sock_path.exists()
            # 验证 health
            r = client.get("/api/health")
            assert r.status_code == 200

        # 退出 lifespan: busd 应该被关, path 文件应该被删
        # 给点时间让 busd 退出
        time.sleep(0.5)
        assert not (tmp_path / DEFAULT_SOCK_PATH_FILE).exists()
