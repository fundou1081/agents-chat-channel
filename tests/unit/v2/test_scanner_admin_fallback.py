"""Tests for Scanner @admin fallback (5 条铁律第 2 条)."""
import pytest
import asyncio
from pathlib import Path

from agents_chat.v2.files.channel import Channel
from agents_chat.v2.files.mailbox import Mailbox
from agents_chat.v2.scanner import Scanner
from agents_chat.v2.state_board import StateBoard


@pytest.fixture
def env(tmp_path):
    """环境: scanner + 频道 + mailboxes."""
    (tmp_path / "mailboxes").mkdir(parents=True, exist_ok=True)
    state_board = StateBoard(tmp_path / "state_board.json")
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir(parents=True, exist_ok=True)
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    # 默认 mailbox 给 "god" (admin)
    Mailbox(tmp_path / "mailboxes" / "god.json", "god")
    return {
        "tmp": tmp_path,
        "state_board": state_board,
        "channels_dir": channels_dir,
        "lock_dir": lock_dir,
    }


class TestResolveAdminFallback:
    """Scanner._resolve_admin_fallback 单元测试 (不需要 async)."""

    def _make_scanner(self, env):
        s = Scanner(
            data_dir=env["tmp"], scan_interval=1.0,
        )
        s.state_board = env["state_board"]
        s.channels_dir = env["channels_dir"]
        s.lock_dir = env["lock_dir"]
        return s

    def test_explicit_channel_admin_keyword(self, env):
        s = self._make_scanner(env)
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_admin("manager")
        assert s._resolve_admin_fallback("频道管理员", ch.list_admins()) == "god"

    def test_explicit_admin_keyword(self, env):
        s = self._make_scanner(env)
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        assert s._resolve_admin_fallback("@admin", ch.list_admins()) == "god"

    def test_explicit_god_keyword(self, env):
        s = self._make_scanner(env)
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        assert s._resolve_admin_fallback("god 帮看下", ch.list_admins()) == "god"

    def test_no_match_fallback_to_first_admin(self, env):
        s = self._make_scanner(env)
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_admin("manager")
        # 没有任何 admin 关键字匹配 → 投第一个 admin
        assert s._resolve_admin_fallback("some_random_word", ch.list_admins()) == "god"

    def test_no_admins_returns_none(self, env):
        s = self._make_scanner(env)
        # 没 admin → 返回 None (不投递)
        assert s._resolve_admin_fallback("频道管理员", []) is None
        assert s._resolve_admin_fallback("anything", []) is None

    def test_admin_name_matches_keyword(self, env):
        """admin 自己的名字含 admin 关键字 → 自动 match."""
        s = self._make_scanner(env)
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("admin_bot")  # 名字本身含 "admin"
        # 新逻辑: admin_bot 必须在 known_agents 里 (有 mailbox)
        Mailbox(env["tmp"] / "mailboxes" / "admin_bot.json", "admin_bot")
        s2 = self._make_scanner(env)  # 重建 scanner 重新发现 agents
        assert s2._resolve_admin_fallback("any_target", ch.list_admins()) == "admin_bot"


class TestScannerRouteAdminFallback:
    """集成测试: mention 没匹配到 agent 时, fallback 到 admin."""

    @pytest.mark.asyncio
    async def test_unmatched_mention_falls_back_to_admin(self, env):
        """@xyz 没匹配到任何 agent → 投递 god (admin).
        关键: msg_from 不是 admin (用 alice), 所以 admin fallback work.
        """
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_member("seller-fish")
        Mailbox(env["tmp"] / "mailboxes" / "seller-fish.json", "seller-fish")

        s = Scanner(data_dir=env["tmp"], scan_interval=1.0)
        s.state_board = env["state_board"]
        s.channels_dir = env["channels_dir"]
        s.lock_dir = env["lock_dir"]
        # msg_from='alice' (不是 admin, admin fallback 才生效)
        ch.append(from_="alice", content="@xyz 帮看下", type="mention", mentions=["xyz"])
        await s._scan_once()
        # god (admin) 应该收到 mention (fallback from xyz)
        god_mb = Mailbox(env["tmp"] / "mailboxes" / "god.json", "god")
        assert len(god_mb.peek()) == 1
        assert god_mb.peek()[0]["type"] == "mention"

    @pytest.mark.asyncio
    async def test_explicit_channel_admin_mention_to_admin(self, env):
        """@频道管理员 → 直接投 admin. msg_from=alice."""
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_member("seller-fish")
        Mailbox(env["tmp"] / "mailboxes" / "seller-fish.json", "seller-fish")

        s = Scanner(data_dir=env["tmp"], scan_interval=1.0)
        s.state_board = env["state_board"]
        s.channels_dir = env["channels_dir"]
        s.lock_dir = env["lock_dir"]
        # msg_from=alice (不是 admin)
        ch.append(from_="alice", content="@频道管理员 帮查报价", type="mention",
                 mentions=["频道管理员"])
        await s._scan_once()
        god_mb = Mailbox(env["tmp"] / "mailboxes" / "god.json", "god")
        assert len(god_mb.peek()) == 1

    @pytest.mark.asyncio
    async def test_god_excluded_from_admin_fallback(self, env):
        """god 发消息, @xyz → 排除 god (自己), 没 admin 可投 → drop."""
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")  # 唯一 admin
        ch.add_member("seller-fish")
        Mailbox(env["tmp"] / "mailboxes" / "seller-fish.json", "seller-fish")

        s = Scanner(data_dir=env["tmp"], scan_interval=1.0)
        s.state_board = env["state_board"]
        s.channels_dir = env["channels_dir"]
        s.lock_dir = env["lock_dir"]
        # msg_from=god (admin 自己), @xyz
        ch.append(from_="god", content="@xyz 帮看下", type="mention", mentions=["xyz"])
        await s._scan_once()
        # god 自己不应该收 (避免自投)
        god_mb = Mailbox(env["tmp"] / "mailboxes" / "god.json", "god")
        assert len(god_mb.peek()) == 0  # 没投, 排除自己

    @pytest.mark.asyncio
    async def test_matched_mention_does_not_double_to_admin(self, env):
        """@sell 匹配到 seller-fish → 只投 seller-fish, 不投 admin."""
        ch = Channel(env["channels_dir"] / "fish.jsonl", "fish")
        ch.add_admin("god")
        ch.add_member("seller-fish")
        Mailbox(env["tmp"] / "mailboxes" / "seller-fish.json", "seller-fish")

        s = Scanner(data_dir=env["tmp"], scan_interval=1.0)
        s.state_board = env["state_board"]
        s.channels_dir = env["channels_dir"]
        s.lock_dir = env["lock_dir"]
        ch.append(from_="god", content="@sell 开价", type="mention", mentions=["sell"])
        await s._scan_once()
        # seller-fish 收到, god 没收到
        seller_mb = Mailbox(env["tmp"] / "mailboxes" / "seller-fish.json", "seller-fish")
        god_mb = Mailbox(env["tmp"] / "mailboxes" / "god.json", "god")
        assert len(seller_mb.peek()) == 1
        assert len(god_mb.peek()) == 0  # 关键: admin 不重复收
