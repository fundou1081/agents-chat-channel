"""
Tests for Channel enabled_workers (worker 白名单).
"""
import pytest
import sys, os, tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../..", "src"))

from agents_chat.v2.files.channel import Channel


class TestChannelEnabledWorkers:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ch_path = os.path.join(self.tmpdir, "test.jsonl")
        self.ch = Channel(self.ch_path, "test")

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_empty_whitelist_all_allowed(self):
        """空白名单=不限制."""
        assert self.ch.list_enabled_workers() == []
        assert self.ch.has_restriction() is False
        assert self.ch.is_enabled("anyone") is True
        assert self.ch.is_enabled("seller-fish") is True

    def test_add_enabled_worker(self):
        assert self.ch.add_enabled_worker("seller-fish") is True
        assert self.ch.add_enabled_worker("seller-fish") is False  # 已存在

        assert self.ch.list_enabled_workers() == ["seller-fish"]
        assert self.ch.has_restriction() is True

    def test_remove_enabled_worker(self):
        self.ch.add_enabled_worker("seller-fish")
        self.ch.add_enabled_worker("buyer-fish")

        assert self.ch.remove_enabled_worker("seller-fish") is True
        assert self.ch.remove_enabled_worker("seller-fish") is False  # 不存在

        assert self.ch.list_enabled_workers() == ["buyer-fish"]

    def test_set_enabled_workers(self):
        self.ch.set_enabled_workers(["a", "b", "c"])
        assert self.ch.list_enabled_workers() == ["a", "b", "c"]

        self.ch.set_enabled_workers([])  # 清空
        assert self.ch.list_enabled_workers() == []
        assert self.ch.has_restriction() is False

    def test_is_enabled(self):
        self.ch.set_enabled_workers(["seller-fish", "buyer-fish"])

        assert self.ch.is_enabled("seller-fish") is True
        assert self.ch.is_enabled("buyer-fish") is True
        assert self.ch.is_enabled("god") is False
        assert self.ch.is_enabled("admin") is False

    def test_reload_preserves_workers(self):
        """重建 Channel 实例后白名单仍然有效."""
        self.ch.set_enabled_workers(["seller-fish", "buyer-fish"])

        ch2 = Channel(self.ch_path, "test")
        assert ch2.list_enabled_workers() == ["seller-fish", "buyer-fish"]
        assert ch2.is_enabled("seller-fish") is True
        assert ch2.is_enabled("god") is False