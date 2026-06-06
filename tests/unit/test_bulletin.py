"""Test Bulletin Board: post, list, claim, relevance, expire."""
from datetime import datetime, timedelta

import pytest

from agents_chat.models import Announcement, Persona
from agents_chat.storage.bulletin_db import BulletinDB


@pytest.fixture
def bulletin(tmp_data_dir):
    db = BulletinDB(tmp_data_dir / "bulletin.db")
    return db


@pytest.mark.asyncio
async def test_post_and_get(bulletin):
    ann = bulletin.new(kind="broadcast", title="周会", body="今天 3pm")
    await bulletin.post(ann)
    got = await bulletin.get(ann.id)
    assert got is not None
    assert got.title == "周会"
    assert got.status == "open"
    assert got.posted_by == "god"  # default


@pytest.mark.asyncio
async def test_list_open(bulletin):
    for i in range(3):
        await bulletin.post(bulletin.new(kind="broadcast", title=f"t{i}", body=""))
    items = await bulletin.list_open()
    assert len(items) == 3
    items = await bulletin.list_open(kind="broadcast")
    assert len(items) == 3
    items = await bulletin.list_open(kind="unassigned_task")
    assert len(items) == 0


@pytest.mark.asyncio
async def test_claim_atomic(bulletin):
    """两人同时 claim, 只能一人成功."""
    ann = bulletin.new(kind="unassigned_task", title="t", body="")
    await bulletin.post(ann)

    ok1, _ = await bulletin.claim(ann.id, "zhang-frontend")
    assert ok1 is True
    # 第二个 claim 应该失败 (race)
    ok2, msg = await bulletin.claim(ann.id, "li-backend")
    assert ok2 is False
    assert "claimed" in msg or "race" in msg

    # verify
    got = await bulletin.get(ann.id)
    assert got.status == "claimed"
    assert got.claimed_by == "zhang-frontend"


@pytest.mark.asyncio
async def test_claim_not_found(bulletin):
    ok, msg = await bulletin.claim("nonexistent-id", "zhang-frontend")
    assert ok is False
    assert "not found" in msg


@pytest.mark.asyncio
async def test_close(bulletin):
    ann = bulletin.new(kind="broadcast", title="t", body="")
    await bulletin.post(ann)
    ok = await bulletin.close(ann.id)
    assert ok
    assert (await bulletin.get(ann.id)).status == "closed"


@pytest.mark.asyncio
async def test_expire_old(bulletin):
    """expired 的自动 expire."""
    ann1 = bulletin.new(kind="broadcast", title="short", body="", expires_in_seconds=0)
    ann2 = bulletin.new(kind="broadcast", title="long", body="", expires_in_seconds=3600)
    await bulletin.post(ann1)
    await bulletin.post(ann2)
    # 强制把 ann1 的 expires_at 改成过去
    ann1_obj = await bulletin.get(ann1.id)
    ann1_obj.expires_at = (datetime.now() - timedelta(hours=1)).isoformat()
    await bulletin.post(ann1_obj)

    n = await bulletin.expire_old()
    assert n >= 1
    assert (await bulletin.get(ann1.id)).status == "expired"
    assert (await bulletin.get(ann2.id)).status == "open"


@pytest.mark.asyncio
async def test_list_for_author_relevance(bulletin):
    """list_for_author 只返回跟 persona 相关的."""
    p = Persona(id="zhang-frontend", display_name="小张", title="前端工程师", workdir="/tmp")
    p2 = Persona(id="li-backend", display_name="小李", title="后端工程师", workdir="/tmp")

    await bulletin.post(bulletin.new(kind="broadcast", title="bcast", body=""))
    await bulletin.post(bulletin.new(kind="unassigned_task", title="前端活", body="", required_role="frontend"))
    await bulletin.post(bulletin.new(kind="unassigned_task", title="后端活", body="", required_role="backend"))

    zhang_items = await bulletin.list_for_author(p, limit=20)
    li_items = await bulletin.list_for_author(p2, limit=20)

    titles_zhang = [i.title for i in zhang_items]
    assert "bcast" in titles_zhang
    assert "前端活" in titles_zhang
    assert "后端活" not in titles_zhang

    titles_li = [i.title for i in li_items]
    assert "bcast" in titles_li
    assert "后端活" in titles_li
    assert "前端活" not in titles_li


@pytest.mark.asyncio
async def test_list_for_author_any_role(bulletin):
    """required_role='any' 任何人都能看."""
    p = Persona(id="anyone", display_name="?", title="any", workdir="/tmp")
    ann = bulletin.new(kind="unassigned_task", title="公共", body="", required_role="any")
    await bulletin.post(ann)
    items = await bulletin.list_for_author(p, limit=20)
    assert any(i.title == "公共" for i in items)


@pytest.mark.asyncio
async def test_list_for_author_empty_role(bulletin):
    """required_role='' (空) 也任何人都能看."""
    p = Persona(id="anyone", display_name="?", title="any", workdir="/tmp")
    ann = bulletin.new(kind="unassigned_task", title="默认", body="", required_role="")
    await bulletin.post(ann)
    items = await bulletin.list_for_author(p, limit=20)
    assert any(i.title == "默认" for i in items)


@pytest.mark.asyncio
async def test_mention_in_body(bulletin):
    """discussion 提到名字 → 匹配."""
    p = Persona(id="zhang-frontend", display_name="小张", title="前端工程师", workdir="/tmp")
    ann = bulletin.new(
        kind="discussion",
        title="API 设计",
        body="@小张 这个 endpoint 的命名规范你来定",
    )
    await bulletin.post(ann)
    items = await bulletin.list_for_author(p, limit=20)
    assert any(i.id == ann.id for i in items)


@pytest.mark.asyncio
async def test_mention_by_id(bulletin):
    p = Persona(id="zhang-frontend", display_name="小张", title="前端", workdir="/tmp")
    ann = bulletin.new(kind="discussion", title="t", body="@zhang-frontend 看一下")
    await bulletin.post(ann)
    items = await bulletin.list_for_author(p, limit=20)
    assert any(i.id == ann.id for i in items)


@pytest.mark.asyncio
async def test_no_match_irrelevant(bulletin):
    p = Persona(id="zhang-frontend", display_name="小张", title="前端", workdir="/tmp")
    ann = bulletin.new(kind="discussion", title="t", body="只有 @li-backend 的事")
    await bulletin.post(ann)
    items = await bulletin.list_for_author(p, limit=20)
    assert not any(i.id == ann.id for i in items)


@pytest.mark.asyncio
async def test_list_claimed_by(bulletin):
    ann = bulletin.new(kind="unassigned_task", title="t", body="")
    await bulletin.post(ann)
    await bulletin.claim(ann.id, "zhang-frontend")
    items = await bulletin.list_claimed_by("zhang-frontend")
    assert len(items) == 1
    assert items[0].id == ann.id


def test_announcement_to_dict_roundtrip():
    ann = Announcement(
        id="abc",
        kind="broadcast",
        title="t",
        body="b",
        posted_by="god",
        posted_at="2026-01-01T00:00:00",
        tags=["x", "y"],
        required_role="frontend",
    )
    d = ann.to_dict()
    ann2 = Announcement.from_dict(d)
    assert ann2.id == ann.id
    assert ann2.title == ann.title
    assert ann2.tags == ["x", "y"]
    assert ann2.required_role == "frontend"
