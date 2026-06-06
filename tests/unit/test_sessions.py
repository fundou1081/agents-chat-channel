"""Test session persistence."""
import pytest
from agents_chat.v1.models import SessionContext, SessionStatus
from agents_chat.v1.storage.session_db import SessionDB


@pytest.mark.asyncio
async def test_upsert_and_get(tmp_data_dir):
    db = SessionDB(tmp_data_dir / "sessions.db")
    s = SessionContext(
        thread_id="T-1",
        topic="重构登录页",
        status="active",
        participants={"god", "zhang"},
        history_ids=["m1", "m2", "m3"],
    )
    await db.upsert("zhang", s)

    got = await db.get("zhang", "T-1")
    assert got is not None
    assert got.topic == "重构登录页"
    assert got.status == "active"
    assert len(got.history_ids) == 3


@pytest.mark.asyncio
async def test_list_active(tmp_data_dir):
    db = SessionDB(tmp_data_dir / "sessions.db")
    s1 = SessionContext(thread_id="T-1", topic="active", status="active")
    s2 = SessionContext(thread_id="T-2", topic="blocked", status="blocked")
    s3 = SessionContext(thread_id="T-3", topic="completed", status="completed")
    await db.upsert("zhang", s1)
    await db.upsert("zhang", s2)
    await db.upsert("zhang", s3)

    active = await db.list_active("zhang")
    assert len(active) == 2
    statuses = {s.status for s in active}
    assert statuses == {"active", "blocked"}


@pytest.mark.asyncio
async def test_update_session_status(tmp_data_dir):
    db = SessionDB(tmp_data_dir / "sessions.db")
    s = SessionContext(thread_id="T-1", topic="x", status="active")
    await db.upsert("zhang", s)
    s.status = "completed"
    s.summary = "做完了"
    await db.upsert("zhang", s)

    got = await db.get("zhang", "T-1")
    assert got.status == "completed"
    assert got.summary == "做完了"
