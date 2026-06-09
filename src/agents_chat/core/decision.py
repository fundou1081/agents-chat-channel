"""
DecisionMaker for v2.0 — LLM 决定 session 续/新建/skip.

设计目标:
  - **单次 LLM call** per mail (用 openai 库, 不调 CLI, 干净解耦)
  - **3 种 action**: continue (用某 session) / new (新建) / skip (忽略)
  - **邮箱路径必答**: is_must_reply=True 时, prompt 强制二选一 (无 skip)
  - **轮询路径 LLM 决定**: is_must_reply=False 时, 三选一
  - **fallback**: LLM 失败/超时 → SessionManager.decide_session (纯程序化)
  - **模型可配置**: 复用 CLI 配置 (default) 或独立配置 (decision-only)

集成:
  - EventHandler.handle_mail 调 decide(mail, sessions, role, is_must_reply)
  - 返回 Decision(action, session_id?, reason?, confidence?)
  - skip → EventHandler 写 system 消息 "忽略"
  - continue/new → EventHandler 续/新建 session, 调 CLI

不用 openai 库的 LLM 类 (如 ChatCompletion) 的便利, 自己构造 prompt + 解析 JSON,
方便测试 + 控制 + 调试.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class DecisionConfig:
    """DecisionMaker 配置.

    默认从环境变量读 (复用 CLI 配置):
      - MINIMAX_BASE_URL / OPENAI_BASE_URL  → base_url
      - MINIMAX_API_KEY / OPENAI_API_KEY    → api_key
      - DECISION_MODEL                      → model (默认 gpt-4o-mini)
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.0
    timeout: float = 10.0
    max_retries: int = 1

    def __post_init__(self):
        # 复用环境变量
        if not self.base_url:
            self.base_url = (
                os.environ.get("DECISION_BASE_URL")
                or os.environ.get("MINIMAX_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or "https://api.openai.com/v1"
            )
        if not self.api_key:
            self.api_key = (
                os.environ.get("DECISION_API_KEY")
                or os.environ.get("MINIMAX_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or ""
            )
        if not self.model:
            self.model = (
                os.environ.get("DECISION_MODEL")
                or os.environ.get("MINIMAX_DECISION_MODEL")
                or "gpt-4o-mini"
            )

    def is_valid(self) -> bool:
        return bool(self.api_key) and bool(self.base_url) and bool(self.model)


@dataclass
class Decision:
    """DecisionMaker 的输出.

    action:
      - "continue": 用现有 session (session_id 必须有)
      - "new": 新建 session (session_id 忽略)
      - "skip": 忽略当前 mail (reason 解释)
    reason: LLM 给的理由, 调试 / 日志用
    confidence: LLM 自信度 high/medium/low (备用, 当前不强制)
    raw: LLM 原始返回 (调试用)
    """
    action: str
    session_id: str = ""
    reason: str = ""
    confidence: str = "medium"
    raw: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# Client Protocol (用于测试 mock)
# =============================================================================


class LLMClient(Protocol):
    """LLM 客户端协议. 测试时可 mock."""

    async def chat(
        self, *, model: str, messages: list[dict], temperature: float, timeout: float,
    ) -> str: ...


class OpenAIClient:
    """真实 openai 库客户端."""

    def __init__(self, base_url: str, api_key: str):
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ImportError(
                "openai library not installed. pip install 'openai>=1.0'"
            ) from e
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)

    async def chat(
        self, *, model: str, messages: list[dict], temperature: float, timeout: float,
    ) -> str:
        resp = await self._client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, timeout=timeout,
        )
        return resp.choices[0].message.content or ""


# =============================================================================
# DecisionMaker
# =============================================================================


class DecisionMaker:
    """LLM 决定 session 续/新建/skip.

    用法:
        cfg = DecisionConfig()
        dm = DecisionMaker(cfg)
        decision = await dm.decide(
            mail={"content": "@bot hi", "extra": {"path": "email"}},
            sessions=[{"session_id": "s1", "topic": "买鱼", "progress": 50, ...}],
            role="你是 buyer-fish",
            is_must_reply=True,
        )
        # decision.action == "continue" or "new" or "skip"
    """

    # 强制 JSON 输出 (LLM 不一定严格遵守, 用正则提取)
    _JSON_RE = re.compile(r"\{[^{}]*\"action\"[^{}]*\}", re.DOTALL)

    def __init__(
        self,
        config: DecisionConfig | None = None,
        client: LLMClient | None = None,
    ):
        self.config = config or DecisionConfig()
        # client 注入 (测试时 mock); 真实用 OpenAIClient
        self._client = client
        if self._client is None and self.config.is_valid():
            self._client = OpenAIClient(
                base_url=self.config.base_url, api_key=self.config.api_key,
            )

    @property
    def is_ready(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def decide(
        self,
        mail: dict,
        sessions: list[dict],
        role: str = "",
        is_must_reply: bool = False,
    ) -> Decision:
        """调 1 次 LLM, 决定 continue/new/skip.

        参数:
          - mail: {content, channel, task_id?, path?: 'email'|'poll'|'broadcast'|'system'}
          - sessions: list of {session_id, topic, channel, progress, content_summary, next_action, ...}
          - role: worker 角色 (system_prompt)
          - is_must_reply: True=邮箱路径 (必答, 二选一); False=轮询路径 (三选一)
        """
        if not self.is_ready:
            raise RuntimeError("DecisionMaker not ready (no client / config invalid)")

        prompt = self._build_prompt(mail, sessions, role, is_must_reply)
        try:
            raw = await self._client.chat(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": "你是 session 路由助手. 只输出 JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.config.temperature,
                timeout=self.config.timeout,
            )
        except Exception as e:
            logger.warning(f"[DecisionMaker] LLM call failed: {e}")
            raise  # 抛出, 让 EventHandler fallback

        return self._parse(raw, sessions, is_must_reply)

    # ------------------------------------------------------------------
    # Prompt 构造
    # ------------------------------------------------------------------

    def _build_prompt(
        self, mail: dict, sessions: list[dict], role: str, is_must_reply: bool,
    ) -> str:
        # sessions 摘要 (避免 prompt 过长)
        sess_lines = []
        for s in sessions[:10]:  # 最多 10 个
            sid = s.get("session_id", "?")
            topic = s.get("topic", "")[:30]
            ch = s.get("channel", "")
            prog = s.get("progress", 0)
            summary = s.get("content_summary", "")[:80]
            next_act = s.get("next_action", "")[:30]
            sess_lines.append(
                f"  - {sid} | {ch} | prog={prog}% | topic='{topic}'\n"
                f"    summary: {summary}\n"
                f"    next_action: {next_act}"
            )
        sessions_str = "\n".join(sess_lines) if sess_lines else "  (无 active session)"

        # 邮箱路径 vs 轮询路径: 不同指令
        if is_must_reply:
            action_desc = (
                "**必须回复** (这是显式 @你的消息).\n"
                "请从 [continue / new] 中选一个, 决定用哪个 session 答:\n"
                "  - continue: 用现有 session (填 session_id)\n"
                "  - new: 新建 session"
            )
        else:
            action_desc = (
                "请从 [continue / new / skip] 中选一个:\n"
                "  - continue: 用现有 session 续 (填 session_id)\n"
                "  - new: 新建 session (新话题)\n"
                "  - skip: 跟我无关, 忽略 (填 reason 简短解释)"
            )

        # 提取 mail 关键信息
        mail_content = (mail.get("content", "") or "")[:300]
        mail_channel = mail.get("channel", "unknown")
        mail_task_id = mail.get("task_id", "")
        mail_path = mail.get("path", "unknown")

        prompt = f"""[Worker 角色]
{role or '(无)'}

[现有 sessions ({len(sessions)} 个 active)]
{sessions_str}

[当前 mail]
path: {mail_path}
channel: {mail_channel}
task_id: {mail_task_id}
content: {mail_content}

[决定]
{action_desc}

[输出格式 - 严格 JSON, 单行]
{{"action": "continue", "session_id": "s_xxx", "reason": "..."}}
或 {{"action": "new", "reason": "..."}}
或 {{"action": "skip", "reason": "..."}}
"""
        return prompt

    # ------------------------------------------------------------------
    # 输出解析
    # ------------------------------------------------------------------

    def _parse(self, raw: str, sessions: list[dict], is_must_reply: bool) -> Decision:
        """解析 LLM 输出. 失败 / 邮箱路径下 skip → 强制改写."""
        raw = (raw or "").strip()

        # 提取 JSON 块
        m = self._JSON_RE.search(raw)
        if not m:
            # 试宽松匹配
            return Decision(
                action="skip" if not is_must_reply else "new",
                reason=f"LLM 输出无法解析: {raw[:100]}",
                raw=raw,
            )
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return Decision(
                action="skip" if not is_must_reply else "new",
                reason=f"JSON 解析失败: {m.group(0)[:100]}",
                raw=raw,
            )

        action = str(data.get("action", "")).lower().strip()
        reason = str(data.get("reason", ""))
        session_id = str(data.get("session_id", ""))

        # 邮箱路径: skip → 强制 new
        if is_must_reply and action == "skip":
            return Decision(
                action="new",
                reason=f"邮箱路径强制必答, LLM 想 skip, 改为 new (原 reason: {reason})",
                raw=raw,
            )

        # continue 但 session_id 无效 → 强制 new
        if action == "continue":
            valid_ids = {s.get("session_id") for s in sessions}
            if not session_id or session_id not in valid_ids:
                return Decision(
                    action="new",
                    reason=f"continue 但 session_id 无效 ('{session_id}'), 改为 new",
                    raw=raw,
                )

        # 非法 action → fallback
        if action not in ("continue", "new", "skip"):
            return Decision(
                action="new",
                reason=f"非法 action '{action}', 改为 new",
                raw=raw,
            )

        return Decision(
            action=action, session_id=session_id, reason=reason, raw=raw,
        )


# =============================================================================

    async def decide_speak(
        self,
        channel_messages: list[dict],
        role: str = "",
        session: dict | None = None,
    ) -> Decision:
        """主动模式: 根据频道最近消息, 决定要不要发言.

        参数:
          - channel_messages: 频道最近消息, list of {from, content, type, ts, ...}
          - role: worker 角色 (system_prompt)
          - session: 当前 session (含 topic/progress/next_action)

        返回:
          - action="speak": 要发言 (content=回复内容, session_id=用哪个 session)
          - action="new": 要发言但新建 session (topic=新话题)
          - action="skip": 不发言, 等下一轮
          - action="initiate": 频道空, 主动发起 (content=发起内容)
        """
        if not self.is_ready:
            raise RuntimeError("DecisionMaker not ready (no client / config invalid)")

        prompt = self._build_speak_prompt(channel_messages, role, session)
        try:
            raw = await self._client.chat(
                model=self.config.model,
                messages=[
                    {"role": "system", "content": "你是频道对话决策助手. 只输出 JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.config.temperature,
                timeout=self.config.timeout,
            )
        except Exception as e:
            raise RuntimeError(f"LLM error during decide_speak: {e}") from e

        return self._parse_speak(raw, channel_messages, session)

    def _build_speak_prompt(
        self, channel_messages: list[dict], role: str, session: dict | None,
    ) -> str:
        """构造 decide_speak 的 prompt."""
        # 格式化频道消息
        if not channel_messages:
            msgs_str = "  (频道无消息, 冷启动)"
        else:
            lines = []
            for m in channel_messages[-10:]:  # 最近 10 条
                frm = m.get("from", "unknown")
                typ = m.get("type", "message")
                txt = (m.get("content", "") or "")[:150]
                ts = m.get("ts", "")
                lines.append(f"  [{ts}] {frm} ({typ}): {txt}")
            msgs_str = "\n".join(lines)

        # session 摘要
        if session:
            sess_str = (
                f"  session_id: {session.get('session_id', '')}\n"
                f"  topic: {session.get('topic', '')}\n"
                f"  progress: {session.get('progress', 0)}%\n"
                f"  next_action: {session.get('next_action', '')}\n"
                f"  content_summary: {(session.get('content_summary', '') or '')[:100]}"
            )
        else:
            sess_str = "  (无 active session)"

        return f"""[Worker 角色]
{role or '(无)'}

[频道最近消息]
{msgs_str}

[当前 session 状态]
{sess_str}

[决定]
你是这个频道的参与者. 请判断你现在应该发言吗?

判断规则:
  - 有跟你相关的新消息 → 应该回复 (action=speak)
  - 消息已结束/成交/无关 → 不发言 (action=skip)
  - 频道空 + 你有明确目标 → 主动发起 (action=initiate)

[输出格式 - 严格 JSON, 单行]
{{"action": "speak", "reason": "...", "content": "你要说的内容"}}
或 {{"action": "skip", "reason": "..."}}
或 {{"action": "initiate", "reason": "...", "topic": "话题", "content": "发起内容"}}
"""

    def _parse_speak(
        self, raw: str, channel_messages: list[dict], session: dict | None,
    ) -> Decision:
        """解析 decide_speak 的 LLM 输出."""
        raw = (raw or "").strip()
        m = self._JSON_RE.search(raw)
        if not m:
            return Decision(
                action="skip",
                reason=f"decide_speak 输出无法解析: {raw[:100]}",
                raw=raw,
            )
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return Decision(
                action="skip",
                reason=f"decide_speak JSON 解析失败",
                raw=raw,
            )

        action = str(data.get("action", "")).lower().strip()
        if action not in ("speak", "skip", "initiate"):
            return Decision(
                action="skip",
                reason=f"decide_speak 非法 action '{action}', 跳过",
                raw=raw,
            )

        return Decision(
            action=action,
            session_id=session.get("session_id", "") if session else "",
            reason=str(data.get("reason", "")),
            raw=raw,
        )



# Public exports
# =============================================================================

__all__ = [
    "DecisionConfig",
    "Decision",
    "DecisionMaker",
    "LLMClient",
    "OpenAIClient",
]
