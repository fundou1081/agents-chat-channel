"""
Think: build prompt for LLM and parse decision.

3-channel architecture:
  - DM (Mailbox): point-to-point
  - Posts (PostsDB): public, role/mention-matched (公告/任务/讨论/临时聊天)
  - Channels (ChannelDB): public, subscription-pushed (持久主题)
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from ..models import Decision, TickContext
from ..llm.mock import MockLLM


THINK_SYSTEM_TEMPLATE = """\
你是 {display_name} ({title})。

# 你的身份
{persona_summary}

# 你的工时
- 上班: {work_hours}
- 工作目录: {workdir}
- 心跳: 每 {heartbeat}s 醒一次

# 你的工具
{tools_list}

# 行为准则
1. 每次心跳醒来, 你有 3 个信息源:
   a. Inbox (DM 邮件, 1-to-1)
   b. Posts (公告/任务/讨论/临时聊天, 你扫到的相关 post)
   c. Channels (订阅的持久频道新消息)
2. 看到内容后, 先判断: 这个跟我相关吗? 要我做什么?
3. 决定后, 4 种 output action:
   a. 发邮件 (outgoing_mail) - DM
   b. 发频道消息 (actions: post_channel_message) - 进 Channel
   c. 认领任务 (actions: claim_post) - 在 Posts kind=task
   d. 调工具 (actions: use_tool) - bash/read/write
4. 一次 tick 内, 每个 action 计数 (受 max_actions_per_tick 限, 默认 3)
5. 不需要每件事都回应。可以 ignore / wait。
6. 如果没新邮件 + 没新 post + 没频道新消息 + 没 active session → next_status="idle"

# 输出格式 (严格 JSON, 不要 markdown 包裹)
{{
  "thinking": "你的内心独白 (中文, 1-2 句话)",
  "actions": [
    {{"type": "use_tool", "payload": {{"tool": "...", "input": "..."}}}},
    {{"type": "claim_post", "payload": {{"id": "<post_id>"}}}},
    {{"type": "post_channel_message", "payload": {{"channel_id": "...", "body": "..."}}}}
  ],
  "outgoing_mail": [
    {{
      "id": "auto",
      "sender": "{persona_id}",
      "recipients": ["<真实 author id>"],
      "thread_id": "<同 thread 邮件共享>",
      "in_reply_to": "<上一封邮件 id, 如果是 reply>",
      "subject": "Re: ...",
      "body": "邮件正文 (中文, 可以多行, 用 \\n)",
      "priority": 5,
      "requires_ack": false
    }}
  ],
  "closed_sessions": ["<thread_id>", ...],
  "next_status": "idle" | "working" | "blocked" | "stalled"
}}

注意:
- outgoing_mail 里的 id 字段可以填 "auto", 系统会生成
- recipients 必须是严格真实 author id (zhang-frontend / li-backend / pm / god)
- 如果你要"思考中但不回信", actions 可以空, outgoing_mail 可以空
- 永远输出合法 JSON
"""


THINK_USER_TEMPLATE = """\
# 当前时间
{now}

# A. Inbox ({n_new} 封新邮件, DM 给你)
{new_mail_block}

# B. Active Sessions ({n_active} 个并行)
{active_sessions_block}

# C. Posts (中央 Posts, {n_posts} 条跟 你相关)
{posts_block}

# D. Channels (订阅频道新消息, {n_channel_msgs} 条)
{channel_msgs_block}

# E. 最近的 memory 召回
{memory_block}

# 你的判断
请基于以上 4 个信息源 (A/B/C/D), 输出 JSON 决策。

- 想私聊: outgoing_mail (A 路径)
- 想参与频道讨论: post_channel_message (D 路径, 给出 channel_id + body)
- 想认领任务: claim_post (C 路径, 给出 post id)
- 任务完成, 关 session: closed_sessions
- 如果 Posts 里有 mention 你的, 必须回应
- 如果 Channel 里有 mention 你的, 必须回应
"""


def build_think_prompt(ctx: TickContext) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) pair for LLM."""

    p = ctx.persona
    if p.sleep_hours is None:
        work_hours = "24/7 (无工时)"
    else:
        work_hours = f"{p.sleep_hours[0]}:00 - {p.sleep_hours[1]}:00 (工时外低频)"

    persona_summary = f"display_name: {p.display_name}\nid: {p.id}\ntitle: {p.title}\nemoji: {p.emoji}"

    tools_list = "(MVP 阶段暂无工具,只发邮件 / 频道消息)"

    system = THINK_SYSTEM_TEMPLATE.format(
        display_name=p.display_name, title=p.title,
        persona_summary=persona_summary, work_hours=work_hours,
        workdir=p.workdir, heartbeat=p.heartbeat_seconds,
        tools_list=tools_list, persona_id=p.id,
    )

    new_mail_block = _format_new_mail(ctx.new_mail)
    active_sessions_block = _format_active_sessions(ctx.active_sessions)
    memory_block = "\n".join(f"- {m}" for m in ctx.memory_recall) or "(无)"
    posts_block = _format_posts(ctx.posts)
    channel_msgs_block = _format_channel_messages(ctx.channel_messages)

    user = THINK_USER_TEMPLATE.format(
        now=datetime.now().isoformat(),
        n_new=len(ctx.new_mail),
        n_active=len(ctx.active_sessions),
        n_posts=len(ctx.posts),
        n_channel_msgs=len(ctx.channel_messages),
        new_mail_block=new_mail_block,
        active_sessions_block=active_sessions_block,
        posts_block=posts_block,
        channel_msgs_block=channel_msgs_block,
        memory_block=memory_block,
    )

    return system, user


def _format_new_mail(mails) -> str:
    if not mails:
        return "(空)"
    lines = []
    for m in mails:
        lines.append(
            f"📬 [{m.created_at.strftime('%H:%M:%S')}] from {m.sender}: {m.subject}\n"
            f"   {m.body[:200]}{'...' if len(m.body) > 200 else ''}\n"
            f"   thread_id={m.thread_id} mail_id={m.id} priority={m.priority}"
        )
    return "\n".join(lines)


def _format_posts(posts) -> str:
    if not posts:
        return "(无跟你相关的开放 post)"
    lines = []
    for p in posts:
        role_tag = f" [role: {p.required_role}]" if p.required_role else ""
        round_tag = f" (round {p.current_round}/{p.max_rounds})" if p.kind == "freechat" else ""
        lines.append(f"📌 [{p.kind}]{role_tag}{round_tag} {p.title}  (id={p.id})")
        if p.body:
            lines.append(f"   {p.body[:200]}")
        if p.tags:
            lines.append(f"   tags: {', '.join(p.tags)}")
    return "\n".join(lines)


def _format_channel_messages(msgs) -> str:
    if not msgs:
        return "(无频道新消息)"
    lines = []
    for m in msgs:
        ch = m.channel_id
        time = m.posted_at.split("T")[-1][:8] if "T" in m.posted_at else m.posted_at
        lines.append(f"💬 [{time}] in #{ch}  {m.sender}: {m.body[:200]}")
        if m.mentions:
            lines.append(f"   mentions: {', '.join(m.mentions)}")
    return "\n".join(lines)


def _format_active_sessions(sessions) -> str:
    if not sessions:
        return "(无 active session)"
    lines = []
    for s in sessions:
        status_emoji = {
            "active": "🟢", "blocked": "🟡", "stalled": "🔴", "completed": "✅"
        }.get(s.status, "⚪")
        block = f" (阻塞: {s.blocked_reason})" if s.blocked_reason else ""
        lines.append(
            f"{status_emoji} [{s.thread_id}] {s.topic}\n"
            f"   status={s.status}{block} | msgs={len(s.history_ids)} | last={s.last_activity.strftime('%H:%M:%S')}\n"
            f"   summary: {s.summary[:150]}"
        )
    return "\n".join(lines)


async def decide(ctx: TickContext, llm) -> Decision:
    """Run LLM and parse decision.

    For mock LLM: pass ctx directly, get Decision back.
    For real LLM: render system+user, parse JSON response.
    """
    system, user = build_think_prompt(ctx)

    if hasattr(llm, 'think') and 'ctx' in llm.think.__code__.co_varnames:
        return await llm.think(system=system, user=user, ctx=ctx, tools=[])

    raw = await llm.think(system=system, user=user, tools=[])
    json_text = _extract_json(raw)
    try:
        d = json.loads(json_text)
    except json.JSONDecodeError as e:
        return Decision(
            thinking=f"LLM 输出无法解析: {e}",
            next_status="idle", raw_response=raw,
        )

    for m in d.get("outgoing_mail", []):
        if m.get("id") == "auto":
            import uuid
            m["id"] = str(uuid.uuid4())[:12]
        if not m.get("created_at") or m["created_at"] == "<用 ISO 格式当前时间>":
            m["created_at"] = datetime.now().isoformat()

    return Decision.from_dict(d)


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text
