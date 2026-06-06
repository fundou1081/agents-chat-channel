"""Test mailbox deliver + fetch."""
import pytest
from agents_chat.models import Mail, MailPriority
from agents_chat.storage.mailbox_db import MailboxDB


@pytest.mark.asyncio
async def test_deliver_and_fetch_unread(tmp_data_dir):
    db = MailboxDB(tmp_data_dir / "mailbox.db")
    m1 = Mail.new(sender="god", recipients=["zhang"], subject="hi", body="hello")
    m2 = Mail.new(sender="pm", recipients=["zhang", "li"], subject="task", body="do X")
    await db.deliver(m1)
    await db.deliver(m2)

    zhang_mail = await db.fetch_unread("zhang")
    assert len(zhang_mail) == 2
    assert zhang_mail[0].sender == "god"

    li_mail = await db.fetch_unread("li")
    assert len(li_mail) == 1
    assert li_mail[0].sender == "pm"


@pytest.mark.asyncio
async def test_mark_read(tmp_data_dir):
    db = MailboxDB(tmp_data_dir / "mailbox.db")
    m = Mail.new(sender="god", recipients=["zhang"], subject="hi", body="x")
    await db.deliver(m)

    unread = await db.fetch_unread("zhang")
    assert len(unread) == 1

    await db.mark_read([m.id])
    unread = await db.fetch_unread("zhang")
    assert len(unread) == 0

    # inbox (含已读) 还有
    inbox = await db.fetch_inbox("zhang")
    assert len(inbox) == 1


@pytest.mark.asyncio
async def test_priority_ordering(tmp_data_dir):
    db = MailboxDB(tmp_data_dir / "mailbox.db")
    low = Mail.new(sender="god", recipients=["zhang"], subject="low", body="x", priority=1)
    high = Mail.new(sender="god", recipients=["zhang"], subject="high", body="x", priority=10)
    normal = Mail.new(sender="god", recipients=["zhang"], subject="normal", body="x", priority=5)
    await db.deliver(low)
    await db.deliver(high)
    await db.deliver(normal)

    mail = await db.fetch_unread("zhang")
    assert mail[0].subject == "high"  # 优先级高排前
