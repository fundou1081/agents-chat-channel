"""
Think: build prompt for LLM and parse decision.

This is the "brain" of the author — given a TickContext, produce a Decision.
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
1. 每次心跳醒来,先看 inbox 有没有新邮件
2. 看到邮件,先判断: 这封跟我相关吗? 谁发的? 要我做什么?
3. 决定: 回信 / 执行任务 / 等别人 / 关闭会话
4. 一次 tick 内,你可以:
   - 发送多封邮件 (outgoing_mail 数组)
   - 执行多个工具调用 (actions 数组)
   - 关闭多个已完成的 session
5. 不需要每封邮件都回。可以 ignore / wait。
6. 如果没有新邮件, 也没 pending 任务 → next_status="idle"

# 输出格式 (严格 JSON)
{{
  "thinking": "你的内心独白 (中文, 1-2 句话)",
  "actions": [
    {{"type": "use_tool", "payload": {{"tool": "...", "input": "..."}}}},
    ...
  ],
  "outgoing_mail": [
    {{
      "id": "auto",
      "sender": "{persona_id}",
      "recipients": ["god" | "pm" | "<persona_id>"],
      "thread_id": "<同 thread 邮件共享>",
      "in_reply_to": "<上一封邮件 id, 如果是 reply>",
      "subject": "Re: ...",
      "body": "邮件正文 (中文, 可以多行, 用 \\n)",
      "attachments": [],
      "priority": 5,
      "requires_ack": false,
      "created_at": "<用 ISO 格式当前时间>",
      "metadata": {{}}
    }}
  ],
  "closed_sessions": ["<thread_id>", ...],
  "next_status": "idle" | "working" | "blocked" | "stalled"
}}

注意:
- outgoing_mail 里的 id 字段可以填 "auto", 系统会生成
- created_at 用 ISO 格式
- 如果你要"思考中但不回信", actions 可以空, outgoing_mail 可以空
- 永远输出合法 JSON, 不要 markdown 包裹
"""


THINK_USER_TEMPLATE = """\
# 当前时间
{now}

# Inbox ({n_new} 封新邮件)
{new_mail_block}

# Active Sessions ({n_active} 个并行)
{active_sessions_block}

# 任务板 (Central Bulletin, {n_bulletins} 条跟 你 相关)
{bulletins_block}

# 最近的 memory 召回
{memory_block}

# 你的判断
请基于以上信息, 输出 JSON 决策。
- outgoing_mail 只能发给你明确的收件人 (recipients 必须是 author id)
- 如果任务板里有 unassigned_task 且 required_role 匹配你, 可以 claim:
  actions 里加 {{"type": "claim_announcement", "payload": {{"id": "<ann_id>"}}}}
- 如果任务板里有 broadcast 跟 你有关, 可以回邮件参与
- 如果任务板里有 discussion mention 你, 必须回邮件
"""


def build_think_prompt(ctx: TickContext) -> tuple[str, str]:
    """Build (system_prompt, user_prompt) pair for LLM."""

    p = ctx.persona
    if p.sleep_hours is None:
        work_hours = "24/7 (无工时)"
    else:
        work_hours = f"{p.sleep_hours[0]}:00 - {p.sleep_hours[1]}:00 (工时外低频)"

    # Persona 摘要
    persona_summary = f"display_name: {p.display_name}\nid: {p.id}\ntitle: {p.title}\nemoji: {p.emoji}"

    # 工具列表 (MVP 暂无真实工具, 留接口)
    tools_list = "(MVP 阶段暂无工具,只发邮件)"

    system = THINK_SYSTEM_TEMPLATE.format(
        display_name=p.display_name,
        title=p.title,
        persona_summary=persona_summary,
        work_hours=work_hours,
        workdir=p.workdir,
        heartbeat=p.heartbeat_seconds,
        tools_list=tools_list,
        persona_id=p.id,
    )

    # 构造 user prompt (context)
    new_mail_block = _format_new_mail(ctx.new_mail)
    active_sessions_block = _format_active_sessions(ctx.active_sessions)
    memory_block = "\n".join(f"- {m}" for m in ctx.memory_recall) or "(无)"
    bulletins_block = _format_bulletins(ctx.bulletins)

    user = THINK_USER_TEMPLATE.format(
        now=datetime.now().isoformat(),
        n_new=len(ctx.new_mail),
        n_active=len(ctx.active_sessions),
        n_bulletins=len(ctx.bulletins),
        new_mail_block=new_mail_block,
        active_sessions_block=active_sessions_block,
        bulletins_block=bulletins_block,
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


def _format_bulletins(bulletins) -> str:
    if not bulletins:
        return "(无跟你相关的开放公告)"
    lines = []
    for b in bulletins:
        role_tag = f" [role: {b.required_role}]" if b.required_role else ""
        lines.append(f"📌 [{b.kind}]{role_tag} {b.title}  (id={b.id})")
        if b.body:
            lines.append(f"   {b.body[:200]}")
        if b.tags:
            lines.append(f"   tags: {', '.join(b.tags)}")
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

    # 检测 LLM 类型 - mock 直接传 ctx
    if hasattr(llm, 'think') and 'ctx' in llm.think.__code__.co_varnames:
        return await llm.think(system=system, user=user, ctx=ctx, tools=[])

    # 真实 LLM: 走 JSON 解析路径
    raw = await llm.think(system=system, user=user, tools=[])
    json_text = _extract_json(raw)
    try:
        d = json.loads(json_text)
    except json.JSONDecodeError as e:
        return Decision(
            thinking=f"LLM 输出无法解析: {e}",
            next_status="idle",
            raw_response=raw,
        )

    # 修复 outgoing_mail 中的占位字段
    for m in d.get("outgoing_mail", []):
        if m.get("id") == "auto":
            import uuid
            m["id"] = str(uuid.uuid4())[:12]
        if not m.get("created_at") or m["created_at"] == "<用 ISO 格式当前时间>":
            m["created_at"] = datetime.now().isoformat()

    return Decision.from_dict(d)


def _extract_json(text: str) -> str:
    """从 LLM 输出中抽取 JSON 块。处理 markdown 包裹和杂质。"""
    # Try direct parse first
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    # Try to find ```json ... ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # Find first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text
