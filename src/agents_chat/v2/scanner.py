"""
Scanner for v2.0 — 纯程序路由 (后台进程 / 线程).

设计文档 3+4: Scanner 负责:
  - 增量读频道 JSONL (用 offset 记录)
  - 解析消息类型 (mention / task_broadcast / reply / status_report)
  - 投递到对应邮箱 (mail.append)
  - 提取 STATUS 块 → 更新 state_board

不调 LLM, 纯程序.

主循环:
  while not stop:
    for ch in channels:
      msgs, new_off = Channel(ch).read_since(offset)
      for msg in msgs:
        route(msg)
      offset = new_off
    sleep(scan_interval)

agent 自动发现: 扫 data_dir/mailboxes/*.json 文件名.
"""
from __future__ import annotations

import asyncio
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .agent import extract_mentions
from .files.channel import Channel
from .files.mailbox import Mailbox
from .state_board import StateBoard
from .status import parse_status_block


# 任务标记
_TASK_TAG_RE = re.compile(r"\[TASK(?:\s+(task[_-]\w+))?\]", re.IGNORECASE)

# Scanner 状态文件 (存各频道 offset)
SCANNER_STATE_FILE = "scanner_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def derive_task_id_from_content(content: str, ref_msg_id: str = "") -> str:
    """从 content 抓 task_id. 用于 task_broadcast 投递时填到邮件的 task_id 字段."""
    # 1. [TASK task_xxx] 标记
    m = _TASK_TAG_RE.search(content or "")
    if m and m.group(1):
        return m.group(1)
    # 2. 内容里的 task_xxx
    m2 = re.search(r"task[_-](\w+)", content or "", re.IGNORECASE)
    if m2:
        full = m2.group(0)
        return full if full.lower().startswith("task_") else f"task_{m2.group(1)}"
    # 3. ref_msg_id
    if ref_msg_id:
        return f"task_{ref_msg_id}"
    # 4. fallback
    import hashlib
    h = hashlib.md5((content or "").encode()).hexdigest()[:8]
    return f"task_auto_{h}"


def fuzzy_resolve_mention(target: str, candidates: list[str]) -> str | None:
    """模糊匹配 target 到 candidates 里的 agent_id.

    规则 (优先级从高到低):
      1. 完全匹配
      2. target 是 candidate 的 prefix (target="sell", candidate="seller-fish") 
      3. target 是 candidate 的子串 (target="fish", candidate="seller-fish")
      4. 多个匹配时: 选**最长**的 candidate (更精确)

    返回匹配到的 agent_id, 无匹配返回 None.
    """
    if not target or not candidates:
        return None
    if target in candidates:
        return target  # 精确匹配
    # prefix / substring 匹配
    matches = []
    for c in candidates:
        if c.startswith(target) or target in c:
            matches.append(c)
    if not matches:
        return None
    # 选最长的 (更精确)
    return max(matches, key=len)


class Scanner:
    """纯程序路由后台进程."""

    def __init__(
        self,
        data_dir: str | Path,
        scan_interval: float = 1.0,
        channel_names: list[str] | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.scan_interval = scan_interval
        self._stop_event = asyncio.Event()

        # 文件 IO
        self.channels_dir = self.data_dir / "channels"
        self.mailboxes_dir = self.data_dir / "mailboxes"
        self.mailboxes_dir.mkdir(parents=True, exist_ok=True)
        self.state_board = StateBoard(self.data_dir / "state_board.json")
        self.state_file = self.data_dir / SCANNER_STATE_FILE

        # 频道列表 (配置 > 扫目录)
        if channel_names:
            self.channel_names = channel_names
        else:
            self.channel_names = self._discover_channels()

        # 频道 offset (从 state file 恢复)
        self.offsets: dict[str, int] = self._load_state()

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    def stop(self):
        self._stop_event.set()

    async def run(self):
        """主循环: 一直跑直到 stop()."""
        # 初始化缺失频道的 offset
        for ch in self.channel_names:
            self.offsets.setdefault(ch, 0)
        self._save_state()

        print(f"[scanner] ▶ run (channels={self.channel_names}, interval={self.scan_interval}s)")
        while not self._stop_event.is_set():
            try:
                await self._scan_once()
            except Exception as e:
                print(f"[scanner] ⚠ scan error: {e}")
                traceback.print_exc()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.scan_interval,
                )
            except asyncio.TimeoutError:
                pass
        print(f"[scanner] ⏹ stopped")

    async def _scan_once(self):
        """扫一次所有频道."""
        any_change = False
        for ch_name in list(self.channel_names):
            ch_path = self.channels_dir / f"{ch_name}.jsonl"
            if not ch_path.exists():
                continue
            ch = Channel(ch_path, ch_name)
            offset = self.offsets.get(ch_name, 0)
            msgs, new_off = ch.read_since(offset)
            if new_off > offset:
                for msg in msgs:
                    await self._route(msg, ch_name)
                self.offsets[ch_name] = new_off
                any_change = True
        if any_change:
            self._save_state()

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    async def _route(self, msg: dict, channel_name: str):
        """路由一条消息.

        步骤 (按设计文档 4):
          1. 解析 mention → 投递到目标邮箱 (模糊匹配)
          2. 检测 [TASK] → 广播给频道成员
          3. 检测 STATUS 块 → 更新 state_board
          4. 提取 task_id (content 里有 [TASK task_xxx])
        """
        msg_id = msg.get("id", "")
        content = msg.get("content", "")
        msg_from = msg.get("from", "")
        msg_type = msg.get("type", "")
        mentions = msg.get("mentions", [])

        # 0. 跳过自己产生的消息 (agent 回复, Scanner 不能再投递给自己)
        if msg_from and self._is_known_agent(msg_from):
            # 但还是要检查 STATUS 块 (Scanner 永远处理 STATUS)
            await self._maybe_update_status(msg)
            return

        # 1. STATUS 块优先 (不管 mention 都有)
        await self._maybe_update_status(msg)

        # 频道成员 (用于 [TASK] 广播 + mention 范围限制)
        ch = self.channel(channel_name)
        members = ch.list_members()
        # 如果频道没声明成员, fallback 到所有已知 agent
        if not members:
            members = self._discover_agents()

        # 2. mention 路由 (模糊匹配 + @admin fallback, 5 条铁律第 2 条)
        if mentions:
            # 频道 admins (用于 fallback)
            admins = ch.list_admins() if hasattr(ch, 'list_admins') else []
            for target in mentions:
                if target == msg_from:
                    continue
                # 模糊匹配: target 跟 members / 已知 agents 匹配
                known = list(set(members + self._discover_agents()))
                resolved = fuzzy_resolve_mention(target, known)
                if not resolved:
                    # 没匹配到任何 agent: 5 条铁律第 2 条 fallback
                    # - 显式 @频道管理员 / @admin / @god → 投 admin
                    # - 其他不确定的 mention → 投频道第一个 admin
                    # - exclude=msg_from: 跳过自己 (避免 god 投给自己)
                    admin_target = self._resolve_admin_fallback(target, admins, exclude=msg_from)
                    if admin_target:
                        resolved = admin_target
                    else:
                        # 没 admin 也没匹配到, 跳过
                        continue
                if resolved == msg_from:
                    continue
                await self._deliver_mail(resolved, "mention", {
                    "ref_msg_id": msg_id,
                    "content": content,
                    "channel": channel_name,
                    "task_id": derive_task_id_from_content(content, msg_id),
                    "extra_mentions": [target],  # 保留原始 mention
                })

        # 3. [TASK] 广播 — 只发给频道成员
        if _TASK_TAG_RE.search(content):
            task_id = derive_task_id_from_content(content, msg_id)
            for agent_id in members:
                if agent_id == msg_from:
                    continue
                # 避免重复: 如果 mention 已经投过, 跳过
                if any(fuzzy_resolve_mention(m, [agent_id]) for m in mentions):
                    continue
                await self._deliver_mail(agent_id, "task_broadcast", {
                    "ref_msg_id": msg_id,
                    "content": content,
                    "channel": channel_name,
                    "task_id": task_id,
                })

    async def _maybe_update_status(self, msg: dict):
        """如果消息含 STATUS 块, 更新 state_board."""
        content = msg.get("content", "")
        status = parse_status_block(content)
        if status and status.is_valid():
            # task_id 来自 STATUS 块, 优先
            task_id = status.task_id
            self.state_board.update_from_status(task_id, {
                "progress": status.progress,
                "summary": status.summary,
                "next_action": status.next_action,
                "confidence": status.confidence,
            }, agent_id=msg.get("from", ""))
            if status.progress >= 100:
                self.state_board.complete(task_id)

    async def _deliver_mail(self, agent_id: str, mail_type: str, mail_data: dict):
        """投递邮件到 agent 邮箱 (agent 必须已注册 mailbox).

        mail_type: "mention" | "task_broadcast" | "opportunity" | "system_notify" | "request_status"
        mail_data: {ref_msg_id, content, channel, task_id?, context_hint?}
        """
        mb = self.mailbox_of(agent_id)
        if not mb.path.exists():
            # 目标 agent 不存在, 跳过
            return
        # 拆出 Mailbox 接受的字段, 其余走 extra
        mailbox_fields = {"ref_msg_id", "type", "content", "channel", "context_hint"}
        kwargs = {"type": mail_type}
        extra = {}
        for k, v in mail_data.items():
            if k in mailbox_fields:
                kwargs[k] = v
            else:
                extra[k] = v
        if extra:
            kwargs["extra"] = extra
        mb.append(**kwargs)

    # ------------------------------------------------------------------
    # Agent 自动发现
    # ------------------------------------------------------------------

    def _discover_agents(self) -> list[str]:
        """扫 data_dir/mailboxes/*.json 文件名 → agent ids."""
        if not self.mailboxes_dir.exists():
            return []
        return sorted([
            p.stem for p in self.mailboxes_dir.glob("*.json")
        ])

    def _resolve_admin_fallback(
        self, target: str, admins: list[str], exclude: Optional[str] = None,
    ) -> Optional[str]:
        """5 条铁律第 2 条: 不确定就 @频道管理员.

        规则:
          1. 显式 @频道管理员 / @admin / @god / @频道 (常见 admin 关键字) → 投到那个 admin
          2. 频道有 admin 但 target 没匹配到任何 agent → 投第一个 admin
          3. 频道没 admin → 返回 None (不投递)
          4. exclude: 排除这个 agent_id (避免 msg_from == admin 投给自己)

        参数:
          - exclude: 排除这个 agent_id (通常是 msg_from, 防止 self-mail)
        """
        if not admins:
            return None
        target_lower = target.lower()
        # 规则 1: 显式提及 admin 关键字
        admin_keywords = ("频道管理员", "admin", "god", "频道", "channel_admin", "manager")
        for admin in admins:
            if exclude and admin == exclude:
                continue  # 跳过自己
            admin_lower = admin.lower()
            for kw in admin_keywords:
                if kw.lower() in target_lower or kw.lower() in admin_lower:
                    return admin
        # 规则 2: fallback 投第一个 admin (排除自己)
        for admin in admins:
            if exclude and admin == exclude:
                continue
            return admin  # 第一个非自己的
        return None

    def _is_known_agent(self, agent_id: str) -> bool:
        """判断 agent_id 是不是会发 reply 的 agent (排除 admin).

        admin (频道元数据里 admins 列表) 是发起者, 不发 reply,
        所以 Scanner 不应该跳过 admin 的消息 (admin 发的是"外部输入", 要正常路由).

        只有 member (不在 admin 列表) 才算 agent, 才会发 reply.
        """
        if agent_id not in self._discover_agents():
            return False
        # 查所有频道元数据, 提取 admins
        admins_global: set[str] = set()
        for ch_path in self.channels_dir.glob("*.jsonl"):
            meta_path = ch_path.with_suffix(ch_path.suffix + ".meta.json")
            if meta_path.exists():
                try:
                    import json
                    meta = json.loads(meta_path.read_text())
                    admins_global.update(meta.get("admins", []))
                except (json.JSONDecodeError, OSError):
                    pass
        return agent_id not in admins_global

    def mailbox_of(self, agent_id: str) -> Mailbox:
        return Mailbox(self.mailboxes_dir / f"{agent_id}.json", agent_id)

    def channel(self, name: str) -> Channel:
        return Channel(self.channels_dir / f"{name}.jsonl", name)

    # ------------------------------------------------------------------
    # 频道发现 + offset 持久化
    # ------------------------------------------------------------------

    def _discover_channels(self) -> list[str]:
        if not self.channels_dir.exists():
            return ["general"]  # 默认一个频道
        return sorted([p.stem for p in self.channels_dir.glob("*.jsonl")]) or ["general"]

    def _load_state(self) -> dict[str, int]:
        if not self.state_file.exists():
            return {}
        import json
        try:
            data = json.loads(self.state_file.read_text("utf-8"))
            offsets = data.get("offsets", {})
            # 强制 int
            return {k: int(v) for k, v in offsets.items()}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self):
        import json
        import os
        import tempfile
        data = {"offsets": self.offsets, "updated_at": _now_iso()}
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, self.state_file)
