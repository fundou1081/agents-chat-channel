"""Unit tests for v2.0 files primitives (lock / channel / mailbox)."""
import json
import tempfile
import time
from pathlib import Path

import pytest

from agents_chat.infra.files import (
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
from agents_chat.infra.files import Channel
from agents_chat.infra.files import Mailbox


class TestLock:
    def test_acquire_release(self, tmp_path):
        p = tmp_path / "task_1.lock"
        assert acquire(p, "agent_a") is True
        info = read_lock_info(p)
        assert info["owner"] == "agent_a"
        assert release(p, "agent_a") is True
        assert not p.exists()

    def test_acquire_already_held(self, tmp_path):
        p = tmp_path / "task_1.lock"
        assert acquire(p, "agent_a") is True
        assert acquire(p, "agent_b") is False  # 已占用
        # 释放后才能 acquire
        assert release(p, "agent_a") is True
        assert acquire(p, "agent_b") is True

    def test_release_wrong_owner(self, tmp_path):
        p = tmp_path / "task_1.lock"
        acquire(p, "agent_a")
        assert release(p, "agent_b") is False  # 不能释放别人的锁
        assert p.exists()  # 锁还在

    def test_expired_detection(self, tmp_path):
        p = tmp_path / "task_1.lock"
        # 创建一个 1 秒 TTL 的锁, 然后 sleep 1.1s
        acquire(p, "agent_a", ttl_seconds=1)
        time.sleep(1.1)
        assert is_expired(p, ttl_seconds=1) is True

    def test_force_release_if_expired(self, tmp_path):
        p = tmp_path / "task_1.lock"
        acquire(p, "agent_a", ttl_seconds=1)
        time.sleep(1.1)
        assert force_release_if_expired(p, ttl_seconds=1) is True
        assert not p.exists()
        # 再 acquire 应该成功
        assert acquire(p, "agent_b", ttl_seconds=1) is True

    def test_refresh_extends_ttl(self, tmp_path):
        p = tmp_path / "task_1.lock"
        acquire(p, "agent_a", ttl_seconds=1)
        time.sleep(0.5)
        assert refresh(p, "agent_a") is True
        time.sleep(0.7)  # 总共 1.2s, 但 refresh 后应该还有 0.5s
        assert is_expired(p, ttl_seconds=1) is False

    def test_is_held_by(self, tmp_path):
        p = tmp_path / "task_1.lock"
        assert is_held_by(p, "agent_a") is False  # 不存在
        acquire(p, "agent_a")
        assert is_held_by(p, "agent_a") is True
        assert is_held_by(p, "agent_b") is False


class TestChannel:
    def test_append_and_count(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.append(from_="alice", content="hi", type="mention")
        ch.append(from_="bob", content="@claude 看", type="mention", mentions=["claude"])
        assert len(ch) == 2

    def test_auto_msg_id(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        m1 = ch.append(from_="alice", content="hi", type="mention")
        m2 = ch.append(from_="bob", content="hey", type="mention")
        assert m1 == "ch_general_1"
        assert m2 == "ch_general_2"

    def test_read_since(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        for i in range(5):
            ch.append(from_="x", content=f"msg{i}", type="mention")
        msgs, new_off = ch.read_since(0)
        assert len(msgs) == 5
        assert new_off == 5
        msgs, new_off = ch.read_since(3)
        assert len(msgs) == 2
        assert msgs[0]["content"] == "msg3"
        assert new_off == 5

    def test_tail(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        for i in range(10):
            ch.append(from_="x", content=f"msg{i}", type="mention")
        tail = ch.tail(3)
        assert len(tail) == 3
        assert tail[0]["content"] == "msg7"
        assert tail[-1]["content"] == "msg9"

    def test_jsonl_format(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.append(from_="alice", content="@claude 数据库", type="mention", mentions=["claude"])
        # 直接读 raw 文件, 验证是合法 JSONL
        with open(ch.path) as f:
            line = f.readline()
        data = json.loads(line)
        assert data["from"] == "alice"
        assert data["mentions"] == ["claude"]

    def test_chinese_content(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.append(from_="alice", content="数据库连接异常 @claude", type="mention", mentions=["claude"])
        msgs = ch.tail(1)
        assert "数据库" in msgs[0]["content"]


class TestMailbox:
    def test_initial_empty(self, tmp_path):
        mb = Mailbox(tmp_path / "qwencode.json", "qwencode")
        assert mb.count() == 0
        assert mb.read_and_clear() == []

    def test_append_and_read(self, tmp_path):
        mb = Mailbox(tmp_path / "qwencode.json", "qwencode")
        mb.append(ref_msg_id="ch_1", type="mention", content="@qwencode hi", channel="general")
        mb.append(ref_msg_id="ch_2", type="task_broadcast", content="[TASK] 写个 hello", channel="general")
        pending = mb.read_and_clear()
        assert len(pending) == 2
        assert pending[0]["type"] == "mention"
        assert pending[1]["type"] == "task_broadcast"
        # 清空后
        assert mb.count() == 0

    def test_peek_does_not_clear(self, tmp_path):
        mb = Mailbox(tmp_path / "qwencode.json", "qwencode")
        mb.append(ref_msg_id="ch_1", type="mention", content="hi", channel="g")
        assert len(mb.peek()) == 1
        assert len(mb.peek()) == 1  # peek 不变

    def test_atomic_replace(self, tmp_path):
        """write 临时文件 + os.replace 原子替换. 验证没残留 .tmp."""
        mb = Mailbox(tmp_path / "qwencode.json", "qwencode")
        for i in range(20):
            mb.append(ref_msg_id=f"ch_{i}", type="mention", content=f"m{i}", channel="g")
        # 目录里不能有 .tmp 残留
        tmp_files = list(tmp_path.glob(".qwencode.json.*.tmp"))
        assert tmp_files == []

    def test_concurrent_append_and_read(self, tmp_path):
        """简单并发: Scanner append + Agent read_and_clear 同时."""
        import threading
        mb = Mailbox(tmp_path / "qwencode.json", "qwencode")

        errors = []
        def writer():
            try:
                for i in range(50):
                    mb.append(ref_msg_id=f"ch_{i}", type="mention", content=f"m{i}", channel="g")
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(10):
                    mb.read_and_clear()
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer)
        t2 = threading.Thread(target=reader)
        t1.start(); t2.start()
        t1.join(); t2.join()
        # 至少不应该崩; 具体数量受 race 影响, 不强求
        assert all(not isinstance(e, (OSError, json.JSONDecodeError)) for e in errors), errors
