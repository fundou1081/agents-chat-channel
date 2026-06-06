"""Test recipient routing / alias resolution."""
import pytest

from agents_chat.v1.author.routing import RECIPIENT_ALIASES, resolve_recipients


class FakeRegistry:
    def __init__(self, authors):
        self.authors = authors

    def get(self, k):
        return self.authors.get(k)


class FakeAuthor:
    def __init__(self, persona_id, display_name="x"):
        self.persona = type("P", (), {"id": persona_id, "display_name": display_name})()


@pytest.fixture
def registry():
    return FakeRegistry({
        "zhang-frontend": FakeAuthor("zhang-frontend", "小张"),
        "li-backend": FakeAuthor("li-backend", "小李"),
        "pm": FakeAuthor("pm", "林经理"),
    })


def test_exact_match(registry):
    """真实 author id 保留."""
    out = resolve_recipients(["zhang-frontend"], registry, persona_id="pm")
    assert out == ["zhang-frontend"]


def test_alias_dev_to_zhang(registry):
    """"dev" → "zhang-frontend"."""
    out = resolve_recipients(["dev"], registry, persona_id="pm")
    assert out == ["zhang-frontend"]


def test_alias_developer(registry):
    """"developer" → "zhang-frontend"."""
    out = resolve_recipients(["developer"], registry, persona_id="pm")
    assert out == ["zhang-frontend"]


def test_alias_team(registry):
    """"team" → "pm"."""
    out = resolve_recipients(["team"], registry, persona_id="zhang")
    assert out == ["pm"]


def test_alias_chinese(registry):
    """"小张" / "前端" → "zhang-frontend"."""
    assert resolve_recipients(["小张"], registry) == ["zhang-frontend"]
    assert resolve_recipients(["前端"], registry) == ["zhang-frontend"]
    assert resolve_recipients(["后端"], registry) == ["li-backend"]


def test_unknown_dropped(registry):
    """未知 recipient drop + warn."""
    out = resolve_recipients(["nonexistent-author"], registry, persona_id="pm")
    assert out == []


def test_fuzzy_match(registry):
    """模糊匹配 (子串)."""
    # "zhang" 是 "zhang-frontend" 的子串
    out = resolve_recipients(["zhang"], registry)
    assert out == ["zhang-frontend"]


def test_fuzzy_match_by_display_name(registry):
    """模糊匹配 (display_name)."""
    out = resolve_recipients(["林"], registry)  # 林经理
    assert out == ["pm"]


def test_dedup(registry):
    """去重."""
    out = resolve_recipients(["zhang-frontend", "dev", "developer"], registry)
    assert out == ["zhang-frontend"]


def test_preserves_order(registry):
    """保留顺序."""
    out = resolve_recipients(["li-backend", "zhang-frontend"], registry)
    assert out == ["li-backend", "zhang-frontend"]


def test_no_registry():
    """没 registry 的话原样返回."""
    out = resolve_recipients(["dev", "zhang-frontend"], None)
    assert out == ["dev", "zhang-frontend"]


def test_god_preserved(registry):
    """"god" 保留 (即使不在 registry 里)."""
    out = resolve_recipients(["god"], registry)
    assert "god" in out


def test_mixed_valid_and_invalid(registry):
    """混合有效和无效."""
    out = resolve_recipients(["zhang-frontend", "bogus", "li-backend"], registry)
    assert out == ["zhang-frontend", "li-backend"]
