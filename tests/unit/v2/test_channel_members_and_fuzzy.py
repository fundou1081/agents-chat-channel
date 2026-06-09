"""Tests for v2.0 Channel members + Scanner fuzzy mention matching."""
import pytest
import asyncio
from pathlib import Path

from agents_chat.v2.infra.files import Channel
from agents_chat.v2.infra.files import Channel, fuzzy_resolve_mention


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

