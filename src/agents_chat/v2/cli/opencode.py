"""
OpenCodeCLI for v2.0 — subprocess 调 opencode CLI.

opencode (https://github.com/sst/opencode) 是 terminal AI agent, 支持
  - `opencode run "prompt" --model <id>` — 单次调用
  - `opencode run "prompt" --session <id>` — resume session
  - `opencode run "prompt" --format json` — 输出 JSONL (每行一个 event)

默认 model: `opencode/minimax-m3-free` (opencode Zen 的 free 模型, 不需 key)

实现: asyncio.create_subprocess_exec 调 opencode 命令, 捕获 stdout (JSONL),
提取 type="text" 的 part 拼成 output_text.

跨平台 (经验来自 v2.0 跨平台跑):
  - 用 shutil.which() 找 CLI binary (Windows 下 .cmd / PowerShell wrapper 路径问题)
  - 路径全用 pathlib.Path (自动 forward slashes on POSIX, backslashes on Windows)
  - 不假设 shell (subprocess_exec args list, 避免 Windows .cmd 解析问题)

workspace_dir: subprocess 在 workspace_dir 里启动, opencode 启动后会自动读
./opencode.md (或 AGENTS.md) 作为角色引导 (per-agent <cli_name>.md 模式).
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional

from .base import CLIResponse, new_session_id


def _find_cli(binary: str) -> str:
    """跨平台找 CLI binary 路径.

    Windows 下 `opencode` 可能是 .cmd / PowerShell wrapper, 需用 shutil.which 找完整路径.
    找不到抛 FileNotFoundError with helpful message.
    """
    found = shutil.which(binary)
    if found:
        return found
    raise FileNotFoundError(
        f"CLI binary '{binary}' not found in PATH. "
        f"Install: see https://github.com/sst/opencode for opencode. "
        f"Or pass full path: OpenCodeCLI(binary='/full/path/to/{binary}')"
    )


class OpenCodeCLI:
    name: str = "opencode"

    def __init__(
        self,
        binary: str = "opencode",
        model: str = "opencode/minimax-m3-free",
        timeout_seconds: int = 300,
    ):
        # 用 shutil.which 找完整路径 (避免 Windows 下 .cmd wrapper 问题)
        self.binary = _find_cli(binary)
        self.model = model
        self.timeout = timeout_seconds
        self.call_count = 0

    async def execute(
        self, prompt: str, session_id: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> CLIResponse:
        start = time.time()
        self.call_count += 1

        # 构造命令
        cmd = [self.binary, "run", prompt, "--model", self.model, "--format", "json"]
        if session_id:
            cmd.extend(["--session", session_id])

        try:
            # 如果提供 workspace_dir, subprocess 在 workspace_dir 里启动
            kwargs = {}
            if workspace_dir:
                kwargs["cwd"] = workspace_dir
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return CLIResponse(
                    output_text="",
                    error=f"opencode timeout after {self.timeout}s",
                    elapsed_ms=int((time.time() - start) * 1000),
                )

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")

            if proc.returncode != 0:
                return CLIResponse(
                    output_text=stdout,
                    error=f"opencode exit {proc.returncode}: {stderr[:500]}",
                    elapsed_ms=int((time.time() - start) * 1000),
                )

            # 解析 JSONL, 提取 type="text" 的 part 拼成 output_text
            output_text = self._extract_text_from_jsonl(stdout)

            # opencode 不直接返回 session id, 我们生成一个
            new_id = None if session_id else new_session_id("oc")

            return CLIResponse(
                output_text=output_text.strip(),
                new_session_id=new_id,
                raw=stdout,
                elapsed_ms=int((time.time() - start) * 1000),
            )
        except FileNotFoundError:
            return CLIResponse(
                output_text="",
                error=f"opencode binary '{self.binary}' not found in PATH",
                elapsed_ms=int((time.time() - start) * 1000),
            )
        except Exception as e:
            return CLIResponse(
                output_text="",
                error=f"opencode exec error: {e}",
                elapsed_ms=int((time.time() - start) * 1000),
            )

    def _extract_text_from_jsonl(self, stdout: str) -> str:
        """从 opencode --format json 的输出中提取 type="text" 的 part.

        输出格式: 每行一个 JSON 对象, 含 type 字段. text 类型的 part 含
        LLM 的实际回复文本. 多个 text part 拼起来.
        """
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # 非 JSON 行 (e.g. log noise), 跳过
                continue
            # 两种结构: 顶层 type="text" 或 nested 在 part 里
            if obj.get("type") == "text" and "text" in obj:
                parts.append(obj["text"])
            elif "part" in obj and obj["part"].get("type") == "text":
                parts.append(obj["part"].get("text", ""))
        return "\n".join(parts) if parts else stdout  # fallback: 原样
