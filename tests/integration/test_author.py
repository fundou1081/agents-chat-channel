"""Test full author lifecycle."""
import asyncio

import pytest
from agents_chat.author.base import Author
from agents_chat.llm.mock import MockLLM
from agents_chat.models import Mail, Persona
from agents_chat.storage.mailbox_db import MailboxDB
from agents_chat.storage.session_db import SessionDB


@pytest.mark.asyncio
async def test_author_full_tick(tmp_data_dir):
    """完整跑一次 tick."""
    mailbox = MailboxDB(tmp_data_dir / "mailbox.db")
    sessions = SessionDB(tmp_data_dir / "sessions.db")
    llm = MockLLM()

    p = Persona(id="zhang", display_name="小张", title="前端", system_prompt="你是小张", heartbeat_seconds=2)
    a = Author(p, mailbox, sessions, llm, data_dir=tmp_data_dir / "logs")

    # 投递邮件
    m = Mail.new(sender="god", recipients=["zhang"], subject="[任务] 改bug", body="请修")
    await mailbox.deliver(m)

    # 跑一次 tick
    await a._tick()

    # 验证
    assert a.total_ticks == 1
    assert a.status in ("idle", "working", "blocked")
    # 邮件已读
    unread = await mailbox.fetch_unread("zhang")
    assert len(unread) == 0

    # 看 mailbox 里所有邮件,应该至少有 1 封 zhang 发出的回信
    import aiosqlite
    import json as _json
    async with aiosqlite.connect(str(tmp_data_dir / "mailbox.db")) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM mails")
        rows = await cursor.fetchall()
    reply_mails = [r for r in rows if r["sender"] == "zhang"]
    assert len(reply_mails) >= 1, f"Expected zhang to send a reply, found {len(reply_mails)} replies in mailbox"
    # session 已创建
    sess = await sessions.list_all("zhang")
    assert len(sess) >= 1
    assert sess[0].thread_id == m.thread_id


@pytest.mark.asyncio
async def test_author_handles_no_mail(tmp_data_dir):
    """没邮件时 tick 是 idle."""
    mailbox = MailboxDB(tmp_data_dir / "mailbox.db")
    sessions = SessionDB(tmp_data_dir / "sessions.db")
    llm = MockLLM()

    p = Persona(id="zhang", display_name="小张", title="前端", system_prompt="...", heartbeat_seconds=2)
    a = Author(p, mailbox, sessions, llm, data_dir=tmp_data_dir / "logs")

    await a._tick()
    assert a.status == "idle"
    assert a.total_ticks == 1


@pytest.mark.asyncio
async def test_two_authors_communicate(tmp_data_dir):
    """两个 author 互相发邮件."""
    mailbox = MailboxDB(tmp_data_dir / "mailbox.db")
    sessions = SessionDB(tmp_data_dir / "sessions.db")
    llm = MockLLM()

    p_zhang = Persona(id="zhang-frontend", display_name="小张", title="前端", system_prompt="...", heartbeat_seconds=2)
    p_li = Persona(id="li-backend", display_name="小李", title="后端", system_prompt="...", heartbeat_seconds=2)
    zhang = Author(p_zhang, mailbox, sessions, llm, data_dir=tmp_data_dir / "logs")
    li = Author(p_li, mailbox, sessions, llm, data_dir=tmp_data_dir / "logs")

    # zhang 发给 li
    m = Mail.new(sender="zhang-frontend", recipients=["li-backend"], subject="API?", body="啥时候给?")
    await mailbox.deliver(m)

    # li tick 一次, 应该回复
    await li._tick()

    # zhang 现在应该收到 li 的回信
    zhang_inbox = await mailbox.fetch_inbox("zhang-frontend")
    assert any(m.sender == "li-backend" for m in zhang_inbox), \
        f"zhang 没收到 li 的回信, zhang inbox: {[m.subject for m in zhang_inbox]}"


@pytest.mark.asyncio
async def test_heartbeat_loop_starts_and_stops(tmp_data_dir):
    """heartbeat loop 能启停."""
    mailbox = MailboxDB(tmp_data_dir / "mailbox.db")
    sessions = SessionDB(tmp_data_dir / "sessions.db")
    llm = MockLLM()

    p = Persona(id="zhang", display_name="小张", title="前端", system_prompt="...", heartbeat_seconds=1)
    a = Author(p, mailbox, sessions, llm, data_dir=tmp_data_dir / "logs")

    await a.start()
    assert a._running
    assert a._heartbeat_task is not None

    # 投递邮件 + 触发 burst
    m = Mail.new(sender="god", recipients=["zhang"], subject="hi", body="hello")
    await mailbox.deliver(m)
    a.trigger_immediate_tick()

    # 等几个 tick
    await asyncio.sleep(2.5)
    assert a.total_ticks >= 1, f"expected ticks >= 1, got {a.total_ticks}"

    # 停
    await a.stop()
    assert not a._running
