"""Integration tests for v2.0 Scanner (纯程序路由)."""
import asyncio
import pytest
from pathlib import Path

from agents_chat.v2.scanner import Scanner, derive_task_id_from_content


class TestDeriveTaskId:
    def test_explicit_in_tag(self):
        assert derive_task_id_from_content("[TASK task_abc] do x") == "task_abc"

    def test_explicit_in_tag_chinese(self):
        assert derive_task_id_from_content("[TASK task_数据库] 修 bug") == "task_数据库"

    def test_in_content(self):
        assert derive_task_id_from_content("process task_xyz now") == "task_xyz"

    def test_no_tag_use_ref(self):
        assert derive_task_id_from_content("hi", ref_msg_id="ch_1") == "task_ch_1"

    def test_hash_fallback(self):
        tid = derive_task_id_from_content("plain text")
        assert tid.startswith("task_auto_")


class TestScannerInit:
    def test_default_channels(self, tmp_path):
        # 没频道文件 → 默认 ['general']
        s = Scanner(tmp_path, channel_names=["general"])
        assert "general" in s.channel_names

    def test_discover_channels(self, tmp_path):
        ch_dir = tmp_path / "channels"
        ch_dir.mkdir(parents=True)
        (ch_dir / "general.jsonl").touch()
        (ch_dir / "random.jsonl").touch()
        s = Scanner(tmp_path)
        assert set(s.channel_names) == {"general", "random"}

    def test_discover_agents(self, tmp_path):
        (tmp_path / "mailboxes").mkdir(parents=True)
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        (tmp_path / "mailboxes" / "claude.json").write_text('{"agent":"claude","pending":[]}')
        s = Scanner(tmp_path, channel_names=["general"])
        assert s._discover_agents() == ["claude", "qwencode"]


class TestScannerRoute:
    @pytest.mark.asyncio
    async def test_mention_route(self, tmp_path):
        """频道里 @qwencode → qwencode 邮箱收到 mention."""
        # 准备
        s = Scanner(tmp_path, channel_names=["general"])
        # 注册 agent (建 mailbox 文件)
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        # god 写频道消息
        ch = s.channel("general")
        ch.append(from_="god", content="@qwencode 处理 task_abc 数据库", type="mention", mentions=["qwencode"])
        # 扫
        await s._scan_once()
        # 验证
        mb = s.mailbox_of("qwencode")
        pending = mb.peek()
        assert len(pending) == 1
        assert pending[0]["type"] == "mention"
        assert pending[0]["task_id"] == "task_abc"
        assert pending[0]["channel"] == "general"

    @pytest.mark.asyncio
    async def test_task_broadcast_route(self, tmp_path):
        """[TASK] 消息 → 所有 agent 收到 task_broadcast."""
        s = Scanner(tmp_path, channel_names=["general"])
        # 3 个 agent
        for aid in ["qwencode", "claude", "opencode"]:
            (tmp_path / "mailboxes" / f"{aid}.json").write_text(f'{{"agent":"{aid}","pending":[]}}')
        # god 发 [TASK]
        ch = s.channel("general")
        ch.append(from_="god", content="[TASK task_broadcast_test] 写个 hello.py", type="task_broadcast")
        # 扫
        await s._scan_once()
        # 验证: 3 个 agent 都有 task_broadcast
        for aid in ["qwencode", "claude", "opencode"]:
            mb = s.mailbox_of(aid)
            pending = mb.peek()
            assert any(m["type"] == "task_broadcast" for m in pending), f"{aid} 没收到"
            broadcast = [m for m in pending if m["type"] == "task_broadcast"][0]
            assert broadcast["task_id"] == "task_broadcast_test"

    @pytest.mark.asyncio
    async def test_status_block_updates_state_board(self, tmp_path):
        """agent reply 含 STATUS → state_board 更新."""
        s = Scanner(tmp_path, channel_names=["general"])
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        # 频道里 reply 含 STATUS
        ch = s.channel("general")
        ch.append(from_="qwencode", content=(
            "处理完了\n\n"
            "<!--STATUS\n"
            " session_id: local_001\n"
            " task_id: task_status_test\n"
            " progress: 80\n"
            " summary: doing\n"
            " next_action: continue\n"
            " confidence: high\n"
            "-->"
        ), type="reply")
        # 扫
        await s._scan_once()
        # 验证
        e = s.state_board.get("task_status_test")
        assert e is not None
        assert e["progress"] == 80
        assert e["agent"] == "qwencode"  # 来自 STATUS 块所在消息的 from

    @pytest.mark.asyncio
    async def test_offset_persists(self, tmp_path):
        """Scanner 重启不重复扫."""
        s1 = Scanner(tmp_path, channel_names=["general"])
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        ch = s1.channel("general")
        ch.append(from_="god", content="@qwencode first", type="mention", mentions=["qwencode"])
        ch.append(from_="god", content="@qwencode second", type="mention", mentions=["qwencode"])
        await s1._scan_once()
        # 2 封 mention 进 qwencode
        assert len(s1.mailbox_of("qwencode").peek()) == 2
        # 重启
        s2 = Scanner(tmp_path, channel_names=["general"])
        await s2._scan_once()
        # qwencode 邮箱没增加 (offset 已记录)
        assert len(s2.mailbox_of("qwencode").peek()) == 2

    @pytest.mark.asyncio
    async def test_skip_own_message(self, tmp_path):
        """agent 自己写频道不会投递给自己."""
        s = Scanner(tmp_path, channel_names=["general"])
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        ch = s.channel("general")
        ch.append(from_="qwencode", content="@qwencode hi myself", type="reply", mentions=["qwencode"])
        await s._scan_once()
        # qwencode 邮箱应该是空的 (不投递给自己)
        assert s.mailbox_of("qwencode").peek() == []

    @pytest.mark.asyncio
    async def test_run_loop(self, tmp_path):
        """run() 主循环跑起来后, 频道新消息自动路由."""
        s = Scanner(tmp_path, channel_names=["general"], scan_interval=0.1)
        (tmp_path / "mailboxes" / "qwencode.json").write_text('{"agent":"qwencode","pending":[]}')
        task = asyncio.create_task(s.run())
        await asyncio.sleep(0.1)
        # 写新消息
        ch = s.channel("general")
        ch.append(from_="god", content="@qwencode hello", type="mention", mentions=["qwencode"])
        # 等路由
        for _ in range(20):
            await asyncio.sleep(0.1)
            if s.mailbox_of("qwencode").peek():
                break
        s.stop()
        await asyncio.wait_for(task, timeout=2.0)
        # 验证
        assert any(m["content"] == "@qwencode hello" for m in s.mailbox_of("qwencode").peek())
