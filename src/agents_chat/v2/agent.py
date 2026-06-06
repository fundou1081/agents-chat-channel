"""
Agent for v2.0 — 主循环: pull 邮箱 + 调 CLI + 写 STATUS.

每个 Agent 是独立进程, 绑一个 CLI 程序 (qwen / opencode / mock).
主循环 (设计文档 5):

```
while active:
    mails = read_and_clear_mailbox()
    if mail empty:
        sleep(poll_interval)
        continue
    for each mail in mails:
        dispatch(mail)
```

处理一封 mail 的流程 (设计文档 11):
  1. 决定 task_id (从 mail 提取)
  2. 检查 type, 决定要不要 claim (mention → 强制, task_broadcast/opportunity → 抢)
  3. acquire 锁 (O_CREAT|O_EXCL, 5min TTL)
  4. state_board.claim (建 entry, 记 claimed_at/heartbeat)
  5. session_index 匹配 (context_hint / find_by_topic) 或新建 local_sess
  6. 构造 prompt, 调 CLI (resume 有 remote_sess 就传, 否则首次)
  7. CLI 返回 reply, 提取 STATUS 块
  8. 写频道 (reply + STATUS)
  9. state_board.update_from_status (progress/summary/...)
  10. 续约锁 (refresh mtime)
  11. 二次路由: reply 里的 @mention → 投递 mention 邮件给目标 agent
"""
from __future__ import annotations

import asyncio
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .cli.base import CLI
from .files.channel import Channel
from .files.lock import (
    acquire as lock_acquire,
    is_held_by as lock_is_held_by,
    refresh as lock_refresh,
    release as lock_release,
)
from .files.mailbox import Mailbox
from .session_index import SessionIndex
from .state_board import StateBoard
from .status import parse_status_block


# mention 提取: @xxx
_MENTION_RE = re.compile(r"@([a-zA-Z][a-zA-Z0-9_-]*)")

# task_broadcast 标记: [TASK]
_TASK_RE = re.compile(r"\[TASK\]", re.IGNORECASE)

# 锁 TTL: 1 小时 (Agent 写 STATUS 时 refresh 续约)
LOCK_TTL_SECONDS = 3600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_mentions(text: str) -> list[str]:
    """从文本提取 @mention 列表 (去重, 保持顺序)."""
    seen = set()
    result = []
    for m in _MENTION_RE.findall(text or ""):
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


class Agent:
    """一个 Agent 进程.

    Usage:
        cli = MockCLI()
        agent = Agent(agent_id="qwencode", cli=cli, data_dir="./data_v2")
        asyncio.create_task(agent.run())  # 后台跑
        ...
        agent.stop()
    """

    def __init__(
        self,
        agent_id: str,
        cli: CLI,
        data_dir: str | Path,
        capabilities: list[str] | None = None,
        poll_interval: float = 2.0,
        default_channel: str = "general",
        system_prompt: str = "",
        workspace_dir: str | Path | None = None,
    ):
        self.agent_id = agent_id
        self.cli = cli
        self.data_dir = Path(data_dir)
        self.capabilities = capabilities or []
        self.poll_interval = poll_interval
        self.default_channel = default_channel
        self.system_prompt = system_prompt
        self._stop_event = asyncio.Event()

        # 文件 IO
        self.mailbox = Mailbox(
            self.data_dir / "mailboxes" / f"{agent_id}.json", agent_id,
        )
        self.session_index = SessionIndex(
            self.data_dir / "sessions" / f"{agent_id}.json", agent_id,
        )
        self.state_board = StateBoard(self.data_dir / "state_board.json")
        self.channels_dir = self.data_dir / "channels"
        self.mailboxes_dir = self.data_dir / "mailboxes"
        self.lock_dir = self.data_dir / "locks"
        self.channels_dir.mkdir(parents=True, exist_ok=True)
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # workspace_dir: 每个 agent 独立工作目录, CLI 在里面启动并读 <cli_name>.md
        if workspace_dir is None:
            workspace_dir = self.data_dir / "workspaces" / agent_id
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        # 启动时写 <cli_name>.md 引导文件
        self._init_workspace_files()

    def _init_workspace_files(self):
        """写 {workspace_dir}/{cli.name}.md 引导文件 (claude.md / opencode.md / qwen.md).

        CLI 工具启动后会自动读工作目录里这个文件作为角色引导.
        如果文件已存在, 保留 (用户可能手动改了).
        """
        md_name = f"{self.cli.name}.md"
        md_path = self.workspace_dir / md_name
        if md_path.exists():
            return
        md_content = self._build_workspace_md()
        md_path.write_text(md_content, encoding="utf-8")

    def _build_workspace_md(self) -> str:
        """构造 {cli_name}.md 引导文件内容."""
        md_name = f"{self.cli.name}.md"
        return f"""# {self.agent_id} 角色定义 (v2.0)

你是 **{self.agent_id}**, 在多 agent 协作网络中工作. 本文件由 Agent 启动时自动生成,
你可以手动修改, Agent 不会覆盖.

## 能力标签

{', '.join(self.capabilities) or '通用'}

## 角色提示 (system_prompt)

{self.system_prompt or '(无)'}

## 工作环境 (相对本文件)

- 频道: `../channels/{{name}}.jsonl` (JSONL, 可读可写)
- 我的邮箱: `../mailboxes/{self.agent_id}.json` (pending 邮件)
- 我的 session 索引: `../sessions/{self.agent_id}.json` (local → remote 映射)
- 任务状态板: `../state_board.json` (全局, 只读)
- 任务锁: `../locks/task_xxx.lock` (5min TTL, mtime 判超时)

## 工作规则

1. **STATUS 块**: 每条 reply 必须嵌入, Scanner 解析用于调度
   ```
   <!--STATUS
    session_id: <local_sess_id>
    task_id: <task_id>
    progress: <0-100>
    summary: <一句话总结>
    next_action: <下一步>
    confidence: <low|medium|high>
   -->
   ```
2. **@mention**: reply 里的 `@xxx` 由 Scanner 自动投递 mention 邮件给 xxx
3. **[TASK] 标记**: 频道消息里的 `[TASK task_xxx]` 会被 Scanner 广播成 task_broadcast
4. **session resume**: 同一 task 多次处理会自动用同一 session_id (本地映射)

## 本文件说明

- 文件名: `{md_name}` (跟 CLI 工具名匹配, e.g. claude.md / opencode.md / qwen.md)
- CLI ({self.cli.name}) 启动后会自动读本文件作为角色引导
- 修改本文件不会影响 Agent 行为 (Agent 不读), 但会影响 CLI 行为
"""

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def channel(self, name: str) -> Channel:
        return Channel(self.channels_dir / f"{name}.jsonl", name)

    def mailbox_of(self, agent_id: str) -> Mailbox:
        return Mailbox(self.mailboxes_dir / f"{agent_id}.json", agent_id)

    def stop(self):
        self._stop_event.set()

    async def run(self):
        """主循环: 一直跑直到 stop()."""
        print(f"[{self.agent_id}] ▶ run (cli={self.cli.name}, poll={self.poll_interval}s)")
        while not self._stop_event.is_set():
            try:
                mails = self.mailbox.read_and_clear()
                if mails:
                    print(f"[{self.agent_id}] 📬 {len(mails)} mail(s)")
                    await self._process_batch(mails)
            except Exception as e:
                print(f"[{self.agent_id}] ⚠ main loop error: {e}")
                traceback.print_exc()
            # sleep (可被 stop 打断)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval,
                )
            except asyncio.TimeoutError:
                pass
        print(f"[{self.agent_id}] ⏹ stopped")

    # ------------------------------------------------------------------
    # 处理邮件
    # ------------------------------------------------------------------

    async def _process_batch(self, mails: list[dict]):
        for mail in mails:
            try:
                await self._process_one(mail)
            except Exception as e:
                print(f"[{self.agent_id}] ⚠ process mail error: {e}")
                traceback.print_exc()

    async def _process_one(self, mail: dict):
        mail_type = mail.get("type", "mention")
        content = mail.get("content", "")
        ref_msg_id = mail.get("ref_msg_id", "")
        channel_name = mail.get("channel", self.default_channel)
        context_hint = mail.get("context_hint", "")
        task_id = mail.get("task_id") or self._derive_task_id(mail)

        # request_status: 调度中心问状态, 调 CLI 重新生成 STATUS
        if mail_type == "request_status":
            await self._handle_request_status(task_id, channel_name, ref_msg_id)
            return

        # system_notify: 不需要 claim, 只在频道回应
        if mail_type == "system_notify":
            await self._handle_system_notify(mail, channel_name, ref_msg_id, content)
            return

        # mention / task_broadcast / opportunity: 尝试 claim
        lock_path = self.lock_dir / f"task_{task_id}.lock"
        if not self._try_claim(lock_path, mail_type):
            print(f"[{self.agent_id}] ⏭ task {task_id} already claimed, skip")
            return

        # claim 成功 → 准备 session + prompt
        local_sess = self._ensure_local_session(task_id, content, channel_name, context_hint)
        entry = self.state_board.get(task_id) or self.state_board.claim(
            task_id, self.agent_id, local_sess, channel=channel_name, ref_msg_id=ref_msg_id,
        )
        # 记录 remote session (如果 claim 时已存在)
        remote_sess = self.session_index.get_remote(local_sess)

        # 构造 prompt (含 system + mail content)
        prompt = self._build_prompt(mail, task_id, channel_name, content)

        # 调 CLI
        print(f"[{self.agent_id}] 🛠 invoke CLI (resume={remote_sess}, workspace={self.workspace_dir})")
        response = await self.cli.invoke(
            prompt,
            resume_session=remote_sess,
            workspace_dir=str(self.workspace_dir),
        )

        if not response.ok:
            err_reply = (
                f"[{self.agent_id}] CLI 错误: {response.error}\n\n"
                f"<!--STATUS\n"
                f" session_id: {local_sess}\n"
                f" task_id: {task_id}\n"
                f" progress: 0\n"
                f" summary: {self.agent_id} CLI 调用失败\n"
                f" next_action: 等待人工介入\n"
                f" confidence: low\n"
                f"-->"
            )
            self.channel(channel_name).append(
                from_=self.agent_id, content=err_reply, type="reply",
                ref_msg_id=ref_msg_id, task_id=task_id,
            )
            return

        # 记录 remote session (首次)
        if not remote_sess and response.new_session_id:
            self.session_index.set_remote(local_sess, response.new_session_id)
            # 同步到 state_board
            if entry:
                entry["remote_session"] = response.new_session_id
                self.state_board.update_from_status(
                    task_id, {}, agent_id=self.agent_id,
                )

        # 写频道
        reply_msg_id = self.channel(channel_name).append(
            from_=self.agent_id,
            content=response.output_text,
            type="reply",
            ref_msg_id=ref_msg_id,
            task_id=task_id,
        )

        # 解析 STATUS + 更新 state_board
        status = parse_status_block(response.output_text)
        if status and status.is_valid():
            self.state_board.update_from_status(task_id, {
                "progress": status.progress,
                "summary": status.summary,
                "next_action": status.next_action,
                "confidence": status.confidence,
            }, agent_id=self.agent_id)
            if status.progress >= 100:
                self.state_board.complete(task_id)
                # 完成 → 释放锁
                lock_release(lock_path, self.agent_id)
                print(f"[{self.agent_id}] ✅ task {task_id} completed")
        else:
            # 没 STATUS 块, 只 touch heartbeat
            self.state_board.touch_heartbeat(task_id)
            print(f"[{self.agent_id}] ⚠ reply 缺 STATUS 块, task={task_id}")

        # 续约锁 (mtime)
        lock_refresh(lock_path, self.agent_id)

        # 二次路由: reply 里的 @mention
        await self._second_route(
            response.output_text, reply_msg_id, task_id, channel_name, local_sess,
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _try_claim(self, lock_path: Path, mail_type: str) -> bool:
        """尝试获取锁. mention → 强制(覆盖), 其他 → 抢."""
        if mail_type == "mention":
            # 检查是不是已经自己持锁
            if lock_is_held_by(lock_path, self.agent_id):
                return True
            return lock_acquire(lock_path, self.agent_id, ttl_seconds=LOCK_TTL_SECONDS)
        # task_broadcast / opportunity: 抢不到就 skip
        if lock_is_held_by(lock_path, self.agent_id):
            return True
        return lock_acquire(lock_path, self.agent_id, ttl_seconds=LOCK_TTL_SECONDS)

    def _ensure_local_session(
        self, task_id: str, content: str, channel: str, context_hint: str,
    ) -> str:
        """确保 task 有对应 local session. 返回 local_id."""
        # 1. context_hint 优先
        if context_hint and self.session_index.get(context_hint):
            return context_hint
        # 2. 看 state_board 里 task 的 session 字段
        entry = self.state_board.get(task_id)
        if entry and entry.get("session"):
            local_id = entry["session"]
            if self.session_index.get(local_id):
                return local_id
        # 3. 按 topic 关键词找
        topic = self._extract_topic(content)
        if topic:
            existing = self.session_index.find_by_topic(topic)
            if existing:
                return existing
        # 4. 新建
        return self.session_index.create(
            topic=topic or task_id, channel=channel,
        )

    def _derive_task_id(self, mail: dict) -> str:
        """从 mail 生成 task_id (没有显式给的时候).

        优先级:
          1. mail 显式 task_id 字段
          2. content 里 [TASK task_xxx] 或 task_xxx 标记
          3. ref_msg_id (如 task_ch_general_100)
          4. hash fallback
        """
        # 1. 显式
        if mail.get("task_id"):
            return mail["task_id"]
        # 2. content 抓
        content = mail.get("content", "")
        m = re.search(r"task[_-](\w+)", content, re.IGNORECASE)
        if m:
            full = m.group(0)
            # 保留 "task_" 前缀 (如果原标记有)
            return full if full.lower().startswith("task_") else f"task_{m.group(1)}"
        # 3. ref_msg_id
        ref = mail.get("ref_msg_id", "")
        if ref:
            return f"task_{ref}"
        # 4. fallback
        import hashlib
        h = hashlib.md5((content + str(time.time())).encode()).hexdigest()[:8]
        return f"task_auto_{h}"

    def _extract_topic(self, content: str) -> str:
        """从 content 抓 topic (前 30 字符, 去 mention/task 标记)."""
        # 去掉 [TASK] / @mention
        text = _TASK_RE.sub("", content or "")
        text = _MENTION_RE.sub("", text)
        text = text.strip().split("\n")[0][:50]
        return text

    def _build_prompt(
        self, mail: dict, task_id: str, channel: str, content: str,
    ) -> str:
        """构造给 CLI 的 prompt."""
        mail_type = mail.get("type", "mention")
        prompt = f"""[System]
你是 {self.agent_id} (能力: {', '.join(self.capabilities) or '通用'}).
{self.system_prompt}

[Task]
task_id: {task_id}
mail_type: {mail_type}
channel: {channel}

[Message]
{content}

[Output]
请处理上述任务, 并在回复末尾嵌入 STATUS 块:
<!--STATUS
 session_id: <你的 local session>
 task_id: {task_id}
 progress: <0-100>
 summary: <一句话总结当前进展>
 next_action: <下一步行动>
 confidence: <low/medium/high>
-->
"""
        return prompt

    async def _handle_request_status(
        self, task_id: str, channel: str, ref_msg_id: str,
    ):
        """处理调度中心的 request_status: 回复一个 STATUS 块 (heartbeat)."""
        entry = self.state_board.get(task_id)
        if not entry:
            return
        status_text = (
            f"<!--STATUS\n"
            f" session_id: {entry.get('session', '')}\n"
            f" task_id: {task_id}\n"
            f" progress: {entry.get('progress', 0)}\n"
            f" summary: {entry.get('summary', '')}\n"
            f" next_action: {entry.get('next_action', '')}\n"
            f" confidence: {entry.get('confidence', 'medium')}\n"
            f"-->"
        )
        self.channel(channel).append(
            from_=self.agent_id, content=status_text, type="status_report",
            ref_msg_id=ref_msg_id, task_id=task_id,
        )
        self.state_board.touch_heartbeat(task_id)

    async def _handle_system_notify(
        self, mail: dict, channel: str, ref_msg_id: str, content: str,
    ):
        """系统通知: 在频道回应 'ack'."""
        self.channel(channel).append(
            from_=self.agent_id, content=f"[ack] {self.agent_id} 收到", type="reply",
            ref_msg_id=ref_msg_id, task_id=mail.get("task_id", ""),
        )

    async def _second_route(
        self, reply_text: str, ref_msg_id: str, task_id: str,
        channel: str, context_hint: str,
    ):
        """提取 reply 里的 @mention, 投递 mention 邮件."""
        mentions = [m for m in extract_mentions(reply_text) if m != self.agent_id]
        for target in mentions:
            mb = self.mailbox_of(target)
            if not mb.path.exists():
                # 目标 agent 不存在, 跳过 (or 可以投递到 god 兜底)
                continue
            mb.append(
                ref_msg_id=ref_msg_id,
                type="mention",
                content=f"@{target} {self.agent_id} 提到你 (task {task_id})",
                channel=channel,
                context_hint=context_hint,
                extra={"task_id": task_id, "from": self.agent_id},
            )
            print(f"[{self.agent_id}] 📨 second-route to {target}")
