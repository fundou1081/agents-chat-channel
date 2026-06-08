"""Tests for v2.0 Worker as Admin.

覆盖:
  - Channel.add_admin(agent_id) 默认 is_worker=True (向后兼容)
  - Channel.add_admin(agent_id, is_worker=False) 写到 human_admins
  - Channel.list_admins() 只返回 worker admins (兼容老 API)
  - Channel.list_human_admins() 返回人类 admins
  - Channel.is_admin(agent_id, is_worker) 精确判断
  - Channel.remove_admin() 移除 admin
  - scanner._resolve_admin_fallback 优先选 worker admin
  - 老 metadata 文件 (没 human_admins / admin_types) 加载时补字段

目标: ≥18 tests, 全部过.
"""
from __future__ import annotations

import json
import pytest
from pathlib import Path


# =============================================================================
# Channel admin 基础
# =============================================================================


class TestChannelAdminBasic:
    def test_add_admin_default_is_worker(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_admin("worker_x") is True
        assert "worker_x" in ch.list_admins()
        assert "worker_x" not in ch.list_human_admins()

    def test_add_admin_explicit_worker(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("worker_x", is_worker=True)
        assert "worker_x" in ch.list_admins()
        assert "worker_x" not in ch.list_human_admins()

    def test_add_admin_human(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_admin("user_ou_abc", is_worker=False) is True
        # 人类 admin 不进 admins 列表
        assert "user_ou_abc" not in ch.list_admins()
        # 单独存
        assert "user_ou_abc" in ch.list_human_admins()

    def test_add_admin_duplicate_worker(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_admin("worker_x") is True
        assert ch.add_admin("worker_x") is False  # 重复

    def test_add_admin_duplicate_human(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_admin("user_ou_abc", is_worker=False) is True
        assert ch.add_admin("user_ou_abc", is_worker=False) is False

    def test_worker_admin_also_member(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("worker_x")
        # add_admin 应当自动 add_member
        assert "worker_x" in ch.list_members()

    def test_human_admin_not_member(self, tmp_path):
        """人类 admin 不在 members 列表里 (他不是 agent, 不需要广播)."""
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("user_ou_abc", is_worker=False)
        assert "user_ou_abc" not in ch.list_members()

    def test_mixed_admins(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("worker_x")
        ch.add_admin("user_ou_abc", is_worker=False)
        ch.add_admin("worker_y")
        # admins 列表: 2 个 worker
        assert sorted(ch.list_admins()) == ["worker_x", "worker_y"]
        # human_admins: 1 个
        assert ch.list_human_admins() == ["user_ou_abc"]


# =============================================================================
# is_admin / remove_admin
# =============================================================================


class TestChannelAdminQueries:
    def test_is_admin_worker(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("worker_x")
        assert ch.is_admin("worker_x", is_worker=True) is True
        assert ch.is_admin("worker_x", is_worker=False) is False
        assert ch.is_admin("worker_x") is True  # 任意类型

    def test_is_admin_human(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("user_ou_abc", is_worker=False)
        assert ch.is_admin("user_ou_abc", is_worker=False) is True
        assert ch.is_admin("user_ou_abc", is_worker=True) is False
        assert ch.is_admin("user_ou_abc") is True

    def test_is_admin_not_admin(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.is_admin("nobody") is False
        assert ch.is_admin("nobody", is_worker=True) is False
        assert ch.is_admin("nobody", is_worker=False) is False

    def test_remove_admin_worker(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("worker_x")
        assert ch.remove_admin("worker_x", is_worker=True) is True
        assert "worker_x" not in ch.list_admins()
        # 重复 remove = False
        assert ch.remove_admin("worker_x", is_worker=True) is False

    def test_remove_admin_human(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("user_ou_abc", is_worker=False)
        assert ch.remove_admin("user_ou_abc", is_worker=False) is True
        assert "user_ou_abc" not in ch.list_human_admins()

    def test_remove_admin_wrong_type(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        ch = Channel(tmp_path / "general.jsonl", "general")
        ch.add_admin("user_ou_abc", is_worker=False)
        # 试图用 is_worker=True 移除 (不匹配)
        assert ch.remove_admin("user_ou_abc", is_worker=True) is False
        # 还在
        assert "user_ou_abc" in ch.list_human_admins()


# =============================================================================
# 老 metadata 兼容
# =============================================================================


class TestChannelMetadataCompat:
    def test_load_legacy_meta(self, tmp_path):
        """老 metadata (没 human_admins / admin_types) 加载时自动补字段."""
        from agents_chat.v2.files.channel import Channel
        # 手动写一个老格式 meta 文件
        meta_path = tmp_path / "general.jsonl.meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "name": "general",
            "members": ["worker_a", "worker_b"],
            "admins": ["worker_a"],
            "created_by": "",
            "created_at": "",
        }))
        # touch jsonl
        (tmp_path / "general.jsonl").touch()
        ch = Channel(tmp_path / "general.jsonl", "general")
        # 老 admins 仍然在
        assert "worker_a" in ch.list_admins()
        # 新字段被补上
        assert ch.list_human_admins() == []
        # 老 members 仍然在
        assert "worker_a" in ch.list_members()
        assert "worker_b" in ch.list_members()

    def test_corrupted_meta_resets(self, tmp_path):
        from agents_chat.v2.files.channel import Channel
        meta_path = tmp_path / "general.jsonl.meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("not valid json {{{")
        (tmp_path / "general.jsonl").touch()
        ch = Channel(tmp_path / "general.jsonl", "general")
        # 不抛异常, 用默认值
        assert ch.list_admins() == []
        assert ch.list_human_admins() == []


# =============================================================================
# scanner fallback 选 worker admin
# =============================================================================

