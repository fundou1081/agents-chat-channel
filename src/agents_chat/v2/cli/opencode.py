"""
OpenCodeCLI for v2.0 — subprocess 调 opencode CLI.

opencode (https://github.com/sst/opencode) 是 terminal AI agent, 支持
  - `opencode run "prompt"` — 单次调用
  - `opencode --session <id> --continue` — resume session

实现: asyncio.create_subprocess_exec 调 opencode 命令, 捕获 stdout.

注意: opencode CLI 真实接口可能跟假设有出入, 部署时需 adjust 命令行.
这里写的是 **最可能** 的接口形式, 实际跑前需要 verify.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from .base import CLIResponse, new_session_id


class OpenCodeCLI:
    name: str = "opencode"

    def __init__(
        self,
        binary: str = "opencode",
        timeout_seconds: int = 300,
    ):
        self.binary = binary
        self.timeout = timeout_seconds
        self.call_count = 0

    async def invoke(
        self, prompt: str, resume_session: Optional[str] = None,
        workspace_dir: Optional[str] = None,
    ) -> CLIResponse:
        start = time.time()
        self.call_count += 1

        # 构造命令
        # opencode run "prompt" [--session <id>]
        cmd = [self.binary, "run", prompt]
        if resume_session:
            cmd.extend(["--session", resume_session])

        try:
            # 如果提供 workspace_dir, subprocess 在 workspace_dir 里启动
            # opencode 启动后会自动读 ./opencode.md (或 AGENTS.md) 作为引导
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

            # opencode 实际不返回 session id, 我们生成一个
            new_id = None if resume_session else new_session_id("oc")

            return CLIResponse(
                output_text=stdout.strip(),
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
