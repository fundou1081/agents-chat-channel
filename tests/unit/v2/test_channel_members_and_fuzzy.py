"""Tests for v2.0 Channel members + Scanner fuzzy mention matching."""
import pytest
import asyncio
from pathlib import Path

from agents_chat.v2.files.channel import Channel
from agents_chat.v2.scanner import fuzzy_resolve_mention, Scanner


class TestChannelMembers:
    def test_default_no_members(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.list_members() == []
        assert ch.list_admins() == []
        assert not ch.is_member("anyone")

    def test_add_member(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_member("seller-fish") is True
        assert ch.add_member("buyer-fish") is True
        # 重复 add 返回 False
        assert ch.add_member("seller-fish") is False
        assert ch.list_members() == ["seller-fish", "buyer-fish"]
        assert ch.is_member("seller-fish")
        assert not ch.is_member("admin")

    def test_add_admin_also_member(self, tmp_path):
        ch = Channel(tmp_path / "general.jsonl", "general")
        assert ch.add_admin("god") is True
        assert ch.list_admins() == ["god"]
        assert ch.list_members() == ["god"]  # admin 自动是 member
        # 再 add 同一个 admin
        assert ch.add_admin("god") is False

    def test_meta_persistence(self, tmp_path):
        p = tmp_path / "fish-market.jsonl"
        ch1 = Channel(p, "fish-market")
        ch1.add_member("seller")
        ch1.add_member("buyer")
        ch1.add_admin("god")  # add_admin 自动 add_member
        # 重启
        ch2 = Channel(p, "fish-market")
        assert ch2.list_members() == ["seller", "buyer", "god"]
        assert ch2.list_admins() == ["god"]

    def test_meta_sidecar_file(self, tmp_path):
        ch = Channel(tmp_path / "fish-market.jsonl", "fish-market")
        ch.add_member("seller")
        # 应该建 .jsonl.meta.json 文件
        meta_path = tmp_path / "fish-market.jsonl.meta.json"
        assert meta_path.exists()
        import json
        meta = json.loads(meta_path.read_text())
        assert meta["members"] == ["seller"]
        assert meta["name"] == "fish-market"


class TestFuzzyResolveMention:
    def test_exact_match(self):
        assert fuzzy_resolve_mention("seller", ["seller", "buyer"]) == "seller"

    def test_prefix_match(self):
        # @sell 应该是 seller-fish (不是 seller)
        assert fuzzy_resolve_mention("sell", ["seller-fish", "buyer-fish"]) == "seller-fish"

    def test_substring_match(self):
        # @fish 匹配 seller-fish 和 buyer-fish, 选最长
        candidates = ["seller-fish", "buyer-fish", "admin"]
        # @fish -> 两个 fish agent 同长, 选 max (按字符序) -> seller-fish
        result = fuzzy_resolve_mention("fish", candidates)
        assert result in ("seller-fish", "buyer-fish")

    def test_longest_wins(self):
        # @s 匹配 seller 和 seller-fish, 选最长
        result = fuzzy_resolve_mention("s", ["seller", "seller-fish", "buyer"])
        assert result == "seller-fish"

    def test_no_match(self):
        assert fuzzy_resolve_mention("z", ["seller", "buyer"]) is None

    def test_empty(self):
        assert fuzzy_resolve_mention("anything", []) is None
        assert fuzzy_resolve_mention("", ["a", "b"]) is None

    def test_exact_beats_prefix(self):
        # @seller 精确匹配 "seller" 而非 "seller-fish"
        # 注意: max(len) 规则下 seller-fish 更长, 但 @seller 精确匹配优先
        # 实际我的实现是先 exact 再模糊, 所以 @seller -> seller
        result = fuzzy_resolve_mention("seller", ["seller", "seller-fish"])
        assert result == "seller"  # exact match wins

    def test_case_sensitive(self):
        # 当前实现大小写敏感
        assert fuzzy_resolve_mention("Seller", ["seller", "buyer"]) is None
        assert fuzzy_resolve_mention("SELLER", ["seller", "buyer"]) is None


class TestScannerFuzzyRoute:
    @pytest.mark.asyncio
    async def test_mention_fuzzy_resolved(self, tmp_path):
        """@sell 模糊匹配到 seller-fish → seller-fish 收到 mention."""
        s = Scanner(tmp_path, channel_names=["fish-market"])
        # 注册 agent + 加 channel member
        (tmp_path / "mailboxes" / "seller-fish.json").write_text('{"agent":"seller-fish","pending":[]}')
        (tmp_path / "mailboxes" / "buyer-fish.json").write_text('{"agent":"buyer-fish","pending":[]}')
        s.channel("fish-market").add_member("seller-fish")
        s.channel("fish-market").add_member("buyer-fish")
        # god 发 @sell (模糊匹配 seller-fish)
        ch = s.channel("fish-market")
        ch.append(from_="god", content="@sell 100 元一条", type="mention", mentions=["sell"])
        await s._scan_once()
        # seller-fish 收到 (buyer-fish 没收到, 因为 @sell 只模糊匹配到 seller-fish)
        assert len(s.mailbox_of("seller-fish").peek()) == 1
        assert s.mailbox_of("buyer-fish").peek() == []

    @pytest.mark.asyncio
    async def test_task_broadcast_to_members_only(self, tmp_path):
        """[TASK] 只广播给频道成员, 不给非成员."""
        s = Scanner(tmp_path, channel_names=["fish-market"])
        # 3 个 agent: seller (成员), buyer (成员), stranger (非成员)
        for aid in ["seller-fish", "buyer-fish", "stranger"]:
            (tmp_path / "mailboxes" / f"{aid}.json").write_text(f'{{"agent":"{aid}","pending":[]}}')
        s.channel("fish-market").add_member("seller-fish")
        s.channel("fish-market").add_member("buyer-fish")
        # god 发 [TASK]
        ch = s.channel("fish-market")
        ch.append(from_="god", content="[TASK task_bargain] 开价 100", type="task_broadcast")
        await s._scan_once()
        # seller + buyer 收到, stranger 没收到
        assert len(s.mailbox_of("seller-fish").peek()) == 1
        assert len(s.mailbox_of("buyer-fish").peek()) == 1
        assert s.mailbox_of("stranger").peek() == []

    @pytest.mark.asyncio
    async def test_no_fuzzy_match_silently_dropped(self, tmp_path):
        """@z 没匹配到任何 agent → 静默不投递."""
        s = Scanner(tmp_path, channel_names=["fish-market"])
        (tmp_path / "mailboxes" / "seller-fish.json").write_text('{"agent":"seller-fish","pending":[]}')
        ch = s.channel("fish-market")
        ch.append(from_="god", content="@z 你好", type="mention", mentions=["z"])
        await s._scan_once()
        # 谁都没收到
        assert s.mailbox_of("seller-fish").peek() == []

    @pytest.mark.asyncio
    async def test_mention_keeps_original_in_extra(self, tmp_path):
        """@sell 模糊匹配到 seller-fish, 邮件里保留原 mention @sell (extra_mentions)."""
        s = Scanner(tmp_path, channel_names=["fish-market"])
        (tmp_path / "mailboxes" / "seller-fish.json").write_text('{"agent":"seller-fish","pending":[]}')
        s.channel("fish-market").add_member("seller-fish")
        ch = s.channel("fish-market")
        ch.append(from_="god", content="@sell 开价", mentions=["sell"])
        await s._scan_once()
        pending = s.mailbox_of("seller-fish").peek()
        assert len(pending) == 1
        # extra_mentions 含原 mention
        assert "sell" in pending[0].get("extra_mentions", [])
