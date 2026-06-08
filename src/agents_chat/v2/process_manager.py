"""
Process Manager for v2.0 Server.

负责启动 / 停止 / 监控 worker 进程 (agent, scanner, scheduler).

设计:
  - 1 个 process 1 个 subprocess.Popen
  - 维护 dict[process_id, ManagedProcess]
  - 提供: list / start / stop / status / logs
  - 状态变化写 state_board.json (供 server 读)

不用 multiprocessing / threading-asyncio 桥接, 直接 subprocess 简单稳定.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ManagedProcess:
    """1 个 managed 进程 (agent / scanner / scheduler).

    字段:
      - process_id: 唯一 id (uuid4 short)
      - kind: "agent" | "scanner" | "scheduler"
      - agent_id: 仅 kind=agent 时有
      - cli: agent 用的 CLI 名字 (mock/qwen/opencode)
      - pid: subprocess.Popen.pid
      - cmd: 启动命令 (list[str])
      - log_path: stdout/stderr 重定向的文件
      - started_at: 启动时间 ISO
      - stopped_at: 停止时间 ISO (空 = 还在跑)
      - exit_code: 退出码 (-1 = 还在跑)
    """

    process_id: str
    kind: str
    agent_id: str = ""
    cli: str = ""
    pid: int = 0
    cmd: list[str] = field(default_factory=list)
    log_path: str = ""
    started_at: str = ""
    stopped_at: str = ""
    exit_code: int = -1

    def is_running(self) -> bool:
        return self.exit_code == -1 and self.pid > 0

    def to_dict(self) -> dict:
        return asdict(self)


# =============================================================================
# ProcessManager
# =============================================================================


class ProcessManager:
    """管理一组 agent / scanner / scheduler 进程.

    状态持久化: data_dir/processes.json (重启用)
    """

    def __init__(self, data_dir: str | Path, log_dir: str | Path | None = None):
        self.data_dir = Path(data_dir).resolve()
        self.log_dir = Path(log_dir) if log_dir else (self.data_dir / "logs")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.processes_file = self.data_dir / "processes.json"
        self._processes: dict[str, ManagedProcess] = {}
        self._popen: dict[str, subprocess.Popen] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_state(self):
        if not self.processes_file.exists():
            return
        try:
            data = json.loads(self.processes_file.read_text("utf-8"))
            for pid, pdata in data.get("processes", {}).items():
                self._processes[pid] = ManagedProcess(**pdata)
        except (json.JSONDecodeError, OSError, TypeError):
            # 损坏文件忽略, 用空状态
            self._processes = {}

    def _save_state(self):
        data = {
            "processes": {
                pid: p.to_dict() for pid, p in self._processes.items()
            },
            "updated_at": _now_iso(),
        }
        tmp = self.processes_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, self.processes_file)

    # ------------------------------------------------------------------
    # 启动 agent
    # ------------------------------------------------------------------

    def start_agent(
        self,
        agent_id: str,
        cli: str = "mock",
        capabilities: list[str] | None = None,
        channel: str = "general",
        system_prompt: str = "",
        workspace_dir: str | None = None,
        poll_interval: float = 2.0,
    ) -> ManagedProcess:
        """启动 1 个 agent 进程.

        返回 ManagedProcess (已记录, pid 已分配).
        重复启动同一个 agent_id → 抛 RuntimeError.
        """
        # 检查重复
        for p in self._processes.values():
            if p.kind == "agent" and p.agent_id == agent_id and p.is_running():
                raise RuntimeError(f"agent {agent_id} already running (pid={p.pid})")

        # 构造命令
        cmd = [
            sys.executable, "-m", "agents_chat.v2.main",
            "run-agent", agent_id,
            "--cli", cli,
            "--data-dir", str(self.data_dir),
            "--channel", channel,
            "--poll-interval", str(poll_interval),
        ]
        if capabilities:
            cmd.extend(["--capabilities", *capabilities])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])
        if workspace_dir:
            cmd.extend(["--workspace-dir", workspace_dir])

        # log 文件
        log_path = self.log_dir / f"agent_{agent_id}_{int(time.time())}.log"

        env = os.environ.copy()
        env["AGENTS_CHAT_DATA_DIR"] = str(self.data_dir)
        # PYTHONPATH 让子进程找到 src/agents_chat 包
        # 项目根 = src/agents_chat/v2/process_manager.py 的 4 级父目录
        project_root = str(Path(__file__).parent.parent.parent.parent)
        env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")

        log_file = open(log_path, "w", encoding="utf-8")
        popen = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=project_root,
        )
        # 注册
        proc = ManagedProcess(
            process_id=uuid.uuid4().hex[:12],
            kind="agent",
            agent_id=agent_id,
            cli=cli,
            pid=popen.pid,
            cmd=cmd,
            log_path=str(log_path),
            started_at=_now_iso(),
            exit_code=-1,
        )
        self._processes[proc.process_id] = proc
        self._popen[proc.process_id] = popen
        self._save_state()
        return proc

    # ------------------------------------------------------------------
    # 启动 scanner / scheduler
    # ------------------------------------------------------------------

    def start_scanner(self, scan_interval: float = 1.0) -> ManagedProcess:
        cmd = [
            sys.executable, "-m", "agents_chat.v2.main",
            "run-scanner",
            "--data-dir", str(self.data_dir),
            "--scan-interval", str(scan_interval),
        ]
        return self._start_simple("scanner", cmd)

    def start_scheduler(self) -> ManagedProcess:
        cmd = [
            sys.executable, "-m", "agents_chat.v2.main",
            "run-scheduler",
            "--data-dir", str(self.data_dir),
        ]
        return self._start_simple("scheduler", cmd)

    def _start_simple(self, kind: str, cmd: list[str]) -> ManagedProcess:
        # 检查重复
        for p in self._processes.values():
            if p.kind == kind and p.is_running():
                raise RuntimeError(f"{kind} already running (pid={p.pid})")
        log_path = self.log_dir / f"{kind}_{int(time.time())}.log"
        env = os.environ.copy()
        env["AGENTS_CHAT_DATA_DIR"] = str(self.data_dir)
        project_root = str(Path(__file__).parent.parent.parent.parent)
        env["PYTHONPATH"] = project_root + os.pathsep + env.get("PYTHONPATH", "")
        log_file = open(log_path, "w", encoding="utf-8")
        popen = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            env=env, cwd=project_root,
        )
        proc = ManagedProcess(
            process_id=uuid.uuid4().hex[:12],
            kind=kind,
            pid=popen.pid,
            cmd=cmd,
            log_path=str(log_path),
            started_at=_now_iso(),
            exit_code=-1,
        )
        self._processes[proc.process_id] = proc
        self._popen[proc.process_id] = popen
        self._save_state()
        return proc

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------

    def stop(self, process_id: str, timeout: float = 5.0) -> bool:
        """停止 1 个进程. SIGTERM → 等 timeout → SIGKILL."""
        p = self._processes.get(process_id)
        if not p:
            return False
        popen = self._popen.get(process_id)
        if popen and popen.poll() is None:
            # 还活着
            try:
                popen.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            # 等
            try:
                popen.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                popen.kill()
                popen.wait()
        p.stopped_at = _now_iso()
        p.exit_code = popen.returncode if popen else 0
        self._popen.pop(process_id, None)
        self._save_state()
        return True

    def stop_by_agent_id(self, agent_id: str) -> bool:
        for pid, p in self._processes.items():
            if p.kind == "agent" and p.agent_id == agent_id and p.is_running():
                return self.stop(pid)
        return False

    def stop_all(self, timeout: float = 5.0):
        for pid in list(self._processes.keys()):
            self.stop(pid, timeout=timeout)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_processes(self, kind: str | None = None) -> list[ManagedProcess]:
        procs = list(self._processes.values())
        if kind:
            procs = [p for p in procs if p.kind == kind]
        return procs

    def get(self, process_id: str) -> Optional[ManagedProcess]:
        return self._processes.get(process_id)

    def is_agent_running(self, agent_id: str) -> bool:
        for p in self._processes.values():
            if p.kind == "agent" and p.agent_id == agent_id and p.is_running():
                return True
        return False

    def is_kind_running(self, kind: str) -> bool:
        for p in self._processes.values():
            if p.kind == kind and p.is_running():
                return True
        return False

    def get_agent_process(self, agent_id: str) -> Optional[ManagedProcess]:
        for p in self._processes.values():
            if p.kind == "agent" and p.agent_id == agent_id:
                return p
        return None

    # ------------------------------------------------------------------
    # 清理已完成进程
    # ------------------------------------------------------------------

    def cleanup_finished(self) -> int:
        """清掉已退出的进程记录. 返回清理数."""
        count = 0
        for pid in list(self._processes.keys()):
            p = self._processes[pid]
            popen = self._popen.get(pid)
            if popen and popen.poll() is not None:
                p.exit_code = popen.returncode
                p.stopped_at = p.stopped_at or _now_iso()
                self._popen.pop(pid, None)
                count += 1
        if count:
            self._save_state()
        return count

    def read_log(self, process_id: str, tail: int = 100) -> str:
        """读 log 文件最后 N 行."""
        p = self._processes.get(process_id)
        if not p or not p.log_path or not Path(p.log_path).exists():
            return ""
        try:
            with open(p.log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            return "".join(lines[-tail:])
        except OSError:
            return ""


__all__ = ["ProcessManager", "ManagedProcess"]
