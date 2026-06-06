"""Test Posts: post, list, claim, relevance, expire."""
from datetime import datetime, timedelta

import pytest

from agents_chat.models import Post, Persona
from agents_chat.storage.posts_db import PostsDB


@pytest.fixture
def posts(tmp_data_dir):
    db = PostsDB(tmp_data_dir / "posts.db")
    return db


@pytest.mark.asyncio
async def test_post_and_get(posts):
    post = posts.new(kind="broadcast", title="周会", body="今天 3pm")
    await posts.post(post)
    got = await posts.get(post.id)
    assert got is not None
    assert got.title == "周会"
    assert got.status == "open"
    assert got.posted_by == "god"  # default


@pytest.mark.asyncio
async def test_list_open(posts):
    for i in range(3):
        await posts.post(posts.new(kind="broadcast", title=f"t{i}", body=""))
    items = await posts.list_open()
    assert len(items) == 3
    items = await posts.list_open(kind="broadcast")
    assert len(items) == 3
    items = await posts.list_open(kind="unassigned_task")
    assert len(items) == 0


@pytest.mark.asyncio
async def test_claim_atomic(posts):
    """两人同时 claim, 只能一人成功."""
    post = posts.new(kind="unassigned_task", title="t", body="")
    await posts.post(post)

    ok1, _ = await posts.claim(post.id, "zhang-frontend")
    assert ok1 is True
    # 第二个 claim 应该失败 (race)
    ok2, msg = await posts.claim(post.id, "li-backend")
    assert ok2 is False
    assert "claimed" in msg or "race" in msg

    # verify
    got = await posts.get(post.id)
    assert got.status == "claimed"
    assert got.claimed_by == "zhang-frontend"


@pytest.mark.asyncio
async def test_claim_not_found(posts):
    ok, msg = await posts.claim("nonexistent-id", "zhang-frontend")
    assert ok is False
    assert "not found" in msg


@pytest.mark.asyncio
async def test_close(posts):
    post = posts.new(kind="broadcast", title="t", body="")
    await posts.post(post)
    ok = await posts.close(post.id)
    assert ok
    assert (await posts.get(post.id)).status == "closed"


@pytest.mark.asyncio
async def test_expire_old(posts):
    """expired 的自动 expire."""
    ann1 = posts.new(kind="broadcast", title="short", body="", expires_in_seconds=0)
    ann2 = posts.new(kind="broadcast", title="long", body="", expires_in_seconds=3600)
    await posts.post(ann1)
    await posts.post(ann2)
    # 强制把 ann1 的 expires_at 改成过去
    ann1_obj = await posts.get(ann1.id)
    ann1_obj.expires_at = (datetime.now() - timedelta(hours=1)).isoformat()
    await posts.post(ann1_obj)

    n = await posts.expire_old()
    assert n >= 1
    assert (await posts.get(ann1.id)).status == "expired"
    assert (await posts.get(ann2.id)).status == "open"


@pytest.mark.asyncio
async def test_list_for_author_relevance(posts):
    """list_for_author 只返回跟 persona 相关的."""
    p = Persona(id="zhang-frontend", display_name="小张", title="前端工程师", workdir="/tmp")
    p2 = Persona(id="li-backend", display_name="小李", title="后端工程师", workdir="/tmp")

    await posts.post(posts.new(kind="broadcast", title="bcast", body=""))
    await posts.post(posts.new(kind="unassigned_task", title="前端活", body="", required_role="frontend"))
    await posts.post(posts.new(kind="unassigned_task", title="后端活", body="", required_role="backend"))

    zhang_items = await posts.list_for_author(p, limit=20)
    li_items = await posts.list_for_author(p2, limit=20)

    titles_zhang = [i.title for i in zhang_items]
    assert "bcast" in titles_zhang
    assert "前端活" in titles_zhang
    assert "后端活" not in titles_zhang

    titles_li = [i.title for i in li_items]
    assert "bcast" in titles_li
    assert "后端活" in titles_li
    assert "前端活" not in titles_li


@pytest.mark.asyncio
async def test_list_for_author_any_role(posts):
    """required_role='any' 任何人都能看."""
    p = Persona(id="anyone", display_name="?", title="any", workdir="/tmp")
    post = posts.new(kind="unassigned_task", title="公共", body="", required_role="any")
    await posts.post(post)
    items = await posts.list_for_author(p, limit=20)
    assert any(i.title == "公共" for i in items)


@pytest.mark.asyncio
async def test_list_for_author_empty_role(posts):
    """required_role='' (空) 也任何人都能看."""
    p = Persona(id="anyone", display_name="?", title="any", workdir="/tmp")
    post = posts.new(kind="unassigned_task", title="默认", body="", required_role="")
    await posts.post(post)
    items = await posts.list_for_author(p, limit=20)
    assert any(i.title == "默认" for i in items)


@pytest.mark.asyncio
async def test_mention_in_body(posts):
    """discussion 提到名字 → 匹配."""
    p = Persona(id="zhang-frontend", display_name="小张", title="前端工程师", workdir="/tmp")
    post = posts.new(
        kind="discussion",
        title="API 设计",
        body="@小张 这个 endpoint 的命名规范你来定",
    )
    await posts.post(post)
    items = await posts.list_for_author(p, limit=20)
    assert any(i.id == post.id for i in items)


@pytest.mark.asyncio
async def test_mention_by_id(posts):
    p = Persona(id="zhang-frontend", display_name="小张", title="前端", workdir="/tmp")
    post = posts.new(kind="discussion", title="t", body="@zhang-frontend 看一下")
    await posts.post(post)
    items = await posts.list_for_author(p, limit=20)
    assert any(i.id == post.id for i in items)


@pytest.mark.asyncio
async def test_no_match_irrelevant(posts):
    p = Persona(id="zhang-frontend", display_name="小张", title="前端", workdir="/tmp")
    post = posts.new(kind="discussion", title="t", body="只有 @li-backend 的事")
    await posts.post(post)
    items = await posts.list_for_author(p, limit=20)
    assert not any(i.id == post.id for i in items)


@pytest.mark.asyncio
async def test_list_claimed_by(posts):
    post = posts.new(kind="unassigned_task", title="t", body="")
    await posts.post(post)
    await posts.claim(post.id, "zhang-frontend")
    items = await posts.list_claimed_by("zhang-frontend")
    assert len(items) == 1
    assert items[0].id == post.id


def test_announcement_to_dict_roundtrip():
    post = Post(
        id="abc",
        kind="broadcast",
        title="t",
        body="b",
        posted_by="god",
        posted_at="2026-01-01T00:00:00",
        tags=["x", "y"],
        required_role="frontend",
    )
    d = post.to_dict()
    ann2 = Post.from_dict(d)
    assert ann2.id == post.id
    assert ann2.title == post.title
    assert ann2.tags == ["x", "y"]
    assert ann2.required_role == "frontend"
