"""Test ChannelDB: channels, members, messages."""
import pytest

from agents_chat.models import Channel, ChannelMessage
from agents_chat.storage.channels_db import ChannelDB


@pytest.fixture
def ch_db(tmp_data_dir):
    return ChannelDB(tmp_data_dir / "channels.db")


@pytest.mark.asyncio
async def test_create_and_get(ch_db):
    ch = ch_db.new_channel(name="#frontend", description="前端技术讨论")
    await ch_db.create_channel(ch)
    got = await ch_db.get(ch.id)
    assert got is not None
    assert got.name == "#frontend"
    assert got.description == "前端技术讨论"


@pytest.mark.asyncio
async def test_duplicate_name_raises(ch_db):
    ch1 = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch1)
    ch2 = ch_db.new_channel(name="#frontend")
    with pytest.raises(ValueError, match="already exists"):
        await ch_db.create_channel(ch2)


@pytest.mark.asyncio
async def test_join_and_leave(ch_db):
    ch = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch)
    ok = await ch_db.join(ch.id, "zhang-frontend")
    assert ok
    # 重复 join 返回 False (already joined)
    ok2 = await ch_db.join(ch.id, "zhang-frontend")
    assert ok2 is False
    # list members
    members = await ch_db.list_members(ch.id)
    assert members == ["zhang-frontend"]


@pytest.mark.asyncio
async def test_leave(ch_db):
    ch = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch)
    await ch_db.join(ch.id, "zhang-frontend")
    await ch_db.join(ch.id, "li-backend")
    ok = await ch_db.leave(ch.id, "zhang-frontend")
    assert ok
    members = await ch_db.list_members(ch.id)
    assert members == ["li-backend"]


@pytest.mark.asyncio
async def test_post_message(ch_db):
    ch = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch)
    msg = ch_db.new_message(channel_id=ch.id, sender="zhang-frontend", body="hello")
    await ch_db.post_message(msg)
    msgs = await ch_db.list_messages(ch.id)
    assert len(msgs) == 1
    assert msgs[0].body == "hello"
    assert msgs[0].sender == "zhang-frontend"


@pytest.mark.asyncio
async def test_parse_mentions_from_body(ch_db):
    """自动从 @ 解析 mentions."""
    ch = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch)
    msg = ch_db.new_message(
        channel_id=ch.id, sender="pm", body="hey @zhang-frontend 看看这个"
    )
    assert "zhang-frontend" in msg.mentions


@pytest.mark.asyncio
async def test_list_messages_with_since(ch_db):
    ch = ch_db.new_channel(name="#frontend")
    await ch_db.create_channel(ch)
    msg = ch_db.new_message(channel_id=ch.id, sender="pm", body="hi")
    await ch_db.post_message(msg)
    # since = now 应该看不到
    import asyncio
    await asyncio.sleep(0.1)
    msgs = await ch_db.list_messages(ch.id, since=__import__("datetime").datetime.now().isoformat())
    assert len(msgs) == 0


@pytest.mark.asyncio
async def test_get_recent_for_authors(ch_db):
    """订阅者批量拉新消息."""
    ch1 = ch_db.new_channel(name="#frontend")
    ch2 = ch_db.new_channel(name="#random")
    await ch_db.create_channel(ch1)
    await ch_db.create_channel(ch2)
    await ch_db.join(ch1.id, "zhang-frontend")
    await ch_db.join(ch2.id, "zhang-frontend")
    await ch_db.join(ch1.id, "li-backend")
    # 张三发消息
    await ch_db.post_message(ch_db.new_message(
        channel_id=ch1.id, sender="pm", body="前端 React 升级"
    ))
    await ch_db.post_message(ch_db.new_message(
        channel_id=ch2.id, sender="pm", body="水群闲聊"
    ))

    # zhang 订阅 #frontend + #random, 应该看到 2 条
    msgs = await ch_db.get_recent_for_authors(["zhang-frontend"], limit=20)
    assert len(msgs) == 2
    # li 只订阅 #frontend, 应该看到 1 条
    msgs = await ch_db.get_recent_for_authors(["li-backend"], limit=20)
    assert len(msgs) == 1
    assert msgs[0].channel_id == ch1.id


@pytest.mark.asyncio
async def test_list_channels_for_author(ch_db):
    ch1 = ch_db.new_channel(name="#frontend")
    ch2 = ch_db.new_channel(name="#random")
    ch3 = ch_db.new_channel(name="#backend")
    await ch_db.create_channel(ch1)
    await ch_db.create_channel(ch2)
    await ch_db.create_channel(ch3)
    await ch_db.join(ch1.id, "zhang-frontend")
    await ch_db.join(ch2.id, "zhang-frontend")
    # zhang 订阅 2 个
    zhang_chs = await ch_db.list_for_author("zhang-frontend")
    assert len(zhang_chs) == 2
    names = {c.name for c in zhang_chs}
    assert names == {"#frontend", "#random"}


@pytest.mark.asyncio
async def test_list_all_channels(ch_db):
    ch1 = ch_db.new_channel(name="#a")
    ch2 = ch_db.new_channel(name="#b")
    await ch_db.create_channel(ch1)
    await ch_db.create_channel(ch2)
    chs = await ch_db.list_channels()
    assert len(chs) == 2
