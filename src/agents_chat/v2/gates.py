"""
Gates for v2.0 — Worker 输入/输出过滤.

每个 gate 是独立的 plug-in, 在 AgentScheduler 接到 mail 后 (input)
或 LLM 生成 reply 后 (output) 调用. 默认空 list, 启用后:
  - 任一 gate 拒绝 → 整个 message 被拒绝
  - 拒绝时, GateChain 返回 (False, sanitized_text, reject_reason)
  - 接受的 gate 可以修改 text (sanitize), 例如截断 / 去敏感

设计目标:
  - 可插拔: 加新 gate 不改 scheduler
  - 可独立开关: scheduler 同时支持 input_gates / output_gates 两组
  - 默认安全: 空 list = 不过滤, 向后兼容
  - 可观测: 每个 gate 有 name, 拒绝时 log + 写频道

用法 (在 main.py 启动时):
    from agents_chat.v2.gates import MaxLengthGate, SecretLeakGate
    scheduler = AgentScheduler(
        ...,
        input_gates=[MaxLengthGate(max_chars=4000)],
        output_gates=[SecretLeakGate(), MaxLengthGate(max_chars=8000)],
    )
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol


# =============================================================================
# Protocol + Result
# =============================================================================


class Gate(Protocol):
    """Worker gate 协议.

    实现要求:
      - name: 唯一标识, 用于日志
      - check_input(text): 在 _build_prompt 前调用
      - check_output(text): 在 _write_channel_reply 前调用
    """

    name: str

    def check_input(self, text: str) -> "GateResult": ...

    def check_output(self, text: str) -> "GateResult": ...


@dataclass
class GateResult:
    """单个 gate 的判定结果.

    - allowed: True = 通过, False = 拒绝
    - text: 改写后的文本 (sanitize), 默认原样
    - reason: 拒绝原因 (allowed=True 时为空)
    - gate: 哪个 gate 做的判定 (调试用)
    """

    allowed: bool
    text: str
    reason: str = ""
    gate: str = ""

    @classmethod
    def allow(cls, text: str, gate: str = "") -> "GateResult":
        return cls(allowed=True, text=text, gate=gate)

    @classmethod
    def deny(cls, text: str, reason: str, gate: str = "") -> "GateResult":
        return cls(allowed=False, text=text, reason=reason, gate=gate)


# =============================================================================
# GateChain: 顺序应用多个 gate
# =============================================================================


@dataclass
class GateChain:
    """按顺序应用多个 gate. 第一个 deny 立即停止 (短路).

    任一 gate 改写了 text, 后续 gate 看到的是改写后的版本.
    任一 gate 拒绝, 整体拒绝 (返回第一个拒绝的 reason).
    """

    gates: list[Gate] = field(default_factory=list)
    direction: str = "input"  # "input" | "output", 仅用于日志

    def run(self, text: str) -> GateResult:
        """顺序跑 gates. 返回第一个 deny 或最终 allow."""
        current = text
        for gate in self.gates:
            # 调对应方向的方法
            method = getattr(gate, f"check_{self.direction}", None)
            if method is None:
                # gate 不支持这个方向, 跳过
                continue
            result = method(current)
            if not result.allowed:
                return GateResult(
                    allowed=False, text=current,
                    reason=f"[{gate.name}] {result.reason}", gate=gate.name,
                )
            # 接受, 用改写后的 text 继续
            current = result.text
        return GateResult(allowed=True, text=current, gate="chain")

    def __len__(self) -> int:
        return len(self.gates)

    def __bool__(self) -> bool:
        return bool(self.gates)


# =============================================================================
# Builtin Gate 1: MaxLengthGate
# =============================================================================


class MaxLengthGate:
    """截断过长内容. 默认 max=8000 (output), 4000 (input).

    截断策略: 在 max_chars 处切断, 加 "...[truncated]" 标记.
    """

    def __init__(self, max_chars: int = 8000, suffix: str = "...[truncated]"):
        self.max_chars = max_chars
        self.suffix = suffix
        self.name = f"max_length_{max_chars}"

    def check_input(self, text: str) -> GateResult:
        return self._check(text)

    def check_output(self, text: str) -> GateResult:
        return self._check(text)

    def _check(self, text: str) -> GateResult:
        if len(text) <= self.max_chars:
            return GateResult.allow(text, self.name)
        truncated = text[: self.max_chars] + self.suffix
        return GateResult(
            allowed=True,  # 截断 = sanitize, 不算拒绝
            text=truncated,
            reason=f"truncated {len(text)} → {len(truncated)} chars",
            gate=self.name,
        )


# =============================================================================
# Builtin Gate 2: SecretLeakGate
# =============================================================================


# 常见 secret 模式 (顺序重要: 长的 / 更具体的 pattern 放在前面)
_SECRET_PATTERNS: list[tuple[str, str]] = [
    # OpenAI / Anthropic / OpenRouter API keys (长的放前面, 短 sk- 会误匹配 sk-ant-)
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "anthropic key"),
    (r"sk-or-v1-[A-Za-z0-9_\-]{20,}", "openrouter key"),
    (r"sk-[A-Za-z0-9_\-]{20,}", "openai-style key"),
    # AWS
    (r"AKIA[0-9A-Z]{16}", "aws access key"),
    (r"aws_secret_access_key\s*=\s*[A-Za-z0-9/+=]{40}", "aws secret"),
    # GitHub
    (r"gh[pousr]_[A-Za-z0-9]{36,255}", "github token"),
    # Generic bearer / password
    (r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", "bearer token"),
    (r"(?i)password\s*[=:]\s*['\"]?[A-Za-z0-9_\-@#$%^&*]{8,}", "password"),
    (r"(?i)api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}", "api key"),
    # 私钥
    (r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "private key"),
]


class SecretLeakGate:
    """检测敏感信息 (API key / 密码 / 私钥) 并 mask.

    默认策略: 替换为 [REDACTED:type] 标记, 仍允许通过.
    可选 strict 模式: 命中则 deny (默认 strict=False).
    """

    def __init__(self, strict: bool = False):
        self.strict = strict
        self.name = "secret_leak"
        self._patterns = [
            (re.compile(p), label) for p, label in _SECRET_PATTERNS
        ]

    def check_input(self, text: str) -> GateResult:
        return self._check(text)

    def check_output(self, text: str) -> GateResult:
        return self._check(text)

    def _check(self, text: str) -> GateResult:
        sanitized = text
        hits: list[str] = []
        for pattern, label in self._patterns:
            if pattern.search(sanitized):
                hits.append(label)
                sanitized = pattern.sub(f"[REDACTED:{label}]", sanitized)
        if not hits:
            return GateResult.allow(text, self.name)
        if self.strict:
            return GateResult.deny(
                text, f"detected secrets: {', '.join(hits)}", self.name,
            )
        # 默认 sanitize + 警告 (不拒绝, 但 log)
        return GateResult(
            allowed=True, text=sanitized,
            reason=f"redacted: {', '.join(hits)}",
            gate=self.name,
        )


# =============================================================================
# Builtin Gate 3: ControlCharsGate
# =============================================================================


class ControlCharsGate:
    """去掉控制字符 (NUL / SOH / 其它不可打印字符), 保留 \\n \\r \\t.

    用途: 防止奇怪的字符让 LLM 或日志解析器炸.
    """

    def __init__(self, keep_whitespace: bool = True):
        self.keep_whitespace = keep_whitespace
        self.name = "control_chars"

    def check_input(self, text: str) -> GateResult:
        return self._check(text)

    def check_output(self, text: str) -> GateResult:
        return self._check(text)

    def _check(self, text: str) -> GateResult:
        cleaned_chars = []
        removed = 0
        for ch in text:
            cat = unicodedata.category(ch)
            # Cc = control, Cf = format, Cn = unassigned
            if cat in ("Cc", "Cf", "Cn"):
                if self.keep_whitespace and ch in "\n\r\t":
                    cleaned_chars.append(ch)
                else:
                    removed += 1
            else:
                cleaned_chars.append(ch)
        cleaned = "".join(cleaned_chars)
        if removed == 0:
            return GateResult.allow(text, self.name)
        return GateResult(
            allowed=True, text=cleaned,
            reason=f"removed {removed} control chars",
            gate=self.name,
        )


# =============================================================================
# Public exports
# =============================================================================

__all__ = [
    "Gate",
    "GateResult",
    "GateChain",
    "MaxLengthGate",
    "SecretLeakGate",
    "ControlCharsGate",
]
