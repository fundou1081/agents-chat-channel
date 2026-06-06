"""
OrchestratorAuthor: 特殊 Author, 负责 DAG 调度 (Phase 3).

关键差异 vs 普通 Author:
  - **不调 LLM**: _tick 纯确定性 (扫 active DAGs + 收回报 + 推进)
  - **构造时多一个 dag_store**: DAG 状态持久化
  - **heartbeat 短** (5s): 调度要实时推进
  - **persona.llm_backend="none"**: 跟 LLM 体系解耦

调度算法:
  1. 处理回报邮件 (扫 inbox): body 含 "DAG_ID=xxx NODE_ID=yyy STATUS=done/failed"
     - done → update node, 检查是否所有 deps 都 done → 推进
     - failed → update node + block_descendants + fail DAG
  2. 扫 active DAGs: 找 ready nodes (deps 都 done) → 派发 (发 Mail 给 assignee)
  3. 检查 DAG 整体完成: 所有 node 都 done → 通知 god, 标 DAG completed
  4. 写 tick log

派发格式 (Mail body):
  ---
  DAG_ID: dag-abc123
  NODE_ID: api
  TITLE: 后端 API
  ---
  
  详细任务说明...
  
  ---
  回执: 完成后回信给 orchestrator, in_reply_to=<本 mail id>
  STATUS: done / failed (加上 -v "错误原因" 解释失败)
  ---

回报格式 (author → orchestrator mail body):
  ---
  DAG_ID: dag-abc123
  NODE_ID: api
  STATUS: done
  ---
  
  完成说明...
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from ..models import Mail, Persona
from ..storage.dag_db import DagStore
from ..storage.mailbox_db import MailboxDB
from .base import Author


# 回报邮件 body 解析 (simple key-value)
_REPORT_RE = re.compile(
    r"DAG_ID:\s*(\S+)\s+NODE_ID:\s*(\S+)\s+STATUS:\s*(done|failed)\s*(.*)",
    re.DOTALL | re.IGNORECASE,
)


class OrchestratorAuthor(Author):
    """DAG 调度器 (不调 LLM).

    仍然继承 Author, 复用 heartbeat_loop / mailbox / status / snapshot.
    只 override _tick (无 LLM) + 不调 decide.
    """

    def __init__(
        self,
        persona: Persona,
        mailbox: MailboxDB,
        sessions,                  # SessionDB
        data_dir: str | None = None,
        registry = None,
        monitor = None,
        rate_limiter = None,
        policy = None,
        posts = None,
        channels = None,
        dag_store: DagStore = None,
    ):
        super().__init__(
            persona=persona, mailbox=mailbox, sessions=sessions,
            llm=None,  # 关键: 不需要 LLM
            data_dir=data_dir, registry=registry, monitor=monitor,
            rate_limiter=rate_limiter, policy=policy,
            posts=posts, channels=channels,
        )
        if dag_store is None:
            raise ValueError("OrchestratorAuthor requires dag_store")
        self.dag_store = dag_store
        self.dispatches_this_tick = 0
        self.reports_handled = 0

    # ========================================================================
    # Tick: 纯确定性调度 (override)
    # ========================================================================

    async def _tick(self):
        self.total_ticks += 1
        self.last_tick_at = datetime.now()
        self.status = "scheduling"
        self.dispatches_this_tick = 0
        self.reports_handled = 0

        try:
            # 1. 收回报邮件
            await self._process_reports()

            # 2. 扫 active DAGs, 派发 ready nodes
            await self._dispatch_ready_nodes()

            # 3. 检查 DAG 整体完成
            await self._check_dag_completion()
        except Exception as e:
            import traceback
            print(f"[orchestrator] tick error: {e}")
            traceback.print_exc()
            self.status = "stalled"
            return

        # activity log
        self.activity_log.append({
            "ts": datetime.now().isoformat(),
            "summary": f"dispatches={self.dispatches_this_tick} reports={self.reports_handled}",
            "status": "scheduling",
            "n_dispatches": self.dispatches_this_tick,
            "n_reports": self.reports_handled,
        })
        self.activity_log = self.activity_log[-100:]

    # ========================================================================
    # Step 1: 处理回报邮件
    # ========================================================================

    async def _process_reports(self):
        reports = await self.mailbox.fetch_unread(
            owner=self.persona.id, since=datetime(1970, 1, 1), limit=50,
        )
        if not reports:
            return
        handled_ids = []
        for mail in reports:
            if await self._handle_one_report(mail):
                handled_ids.append(mail.id)
                self.reports_handled += 1
        if handled_ids:
            await self.mailbox.mark_read(handled_ids)

    async def _handle_one_report(self, mail: Mail) -> bool:
        """解析回报邮件并更新 DAG node. 返回 True 表示成功处理."""
        m = _REPORT_RE.search(mail.body or "")
        if not m:
            print(f"[orchestrator] report parse failed: from={mail.sender} subject={mail.subject}")
            return False
        dag_id, node_id, status, rest = m.group(1), m.group(2), m.group(3).lower(), m.group(4).strip()
        # 安全: 只能更新自己 DAG 里的 node
        dag = await self.dag_store.get_dag(dag_id)
        if not dag:
            print(f"[orchestrator] report for unknown DAG: {dag_id}")
            return False
        node = dag.get_node(node_id)
        if not node:
            print(f"[orchestrator] report for unknown node: {dag_id}/{node_id}")
            return False
        # idempotent: 已经是终态, 忽略
        if node.status in ("done", "failed", "blocked"):
            return True
        if status == "done":
            await self.dag_store.update_node_status(
                dag_id, node_id, "done",
                report_mail_id=mail.id,
            )
        elif status == "failed":
            err = rest or "no reason given"
            await self.dag_store.update_node_status(
                dag_id, node_id, "failed",
                report_mail_id=mail.id,
                error=err,
            )
            # block 下游
            await self.dag_store.block_descendants(dag_id, node_id)
        return True

    # ========================================================================
    # Step 2: 派发 ready nodes
    # ========================================================================

    async def _dispatch_ready_nodes(self):
        active = await self.dag_store.list_active_dags()
        for dag in active:
            ready = dag.ready_nodes()
            for node in ready:
                if self.dispatches_this_tick >= self.policy.max_actions_per_tick:
                    return
                if not node.assignee:
                    print(f"[orchestrator] node {dag.id}/{node.id} has no assignee, skip")
                    continue
                # 发 Mail
                mail = self._build_dispatch_mail(dag, node)
                await self.mailbox.deliver(mail)
                # 标 running (同时记录 dispatch_mail_id, 避免二次 update 覆盖 status)
                await self.dag_store.update_node_status(
                    dag.id, node.id, "running",
                    started_at=datetime.now().isoformat(),
                    timeout_at=(datetime.now() + timedelta(hours=1)).isoformat(),
                    dispatch_mail_id=mail.id,
                )
                self.dispatches_this_tick += 1
                if self.monitor:
                    self.monitor.mail_sent(mail, by_author=self.persona.id)

    def _build_dispatch_mail(self, dag, node) -> Mail:
        """构造派发邮件 (Mailbox DM, 1-to-1)."""
        body = f"""---
DAG_ID: {dag.id}
NODE_ID: {node.id}
TITLE: {node.title or node.id}
---

{node.body or '(无详细说明)'}

---
回执: 完成后回信给 orchestrator, in_reply_to=<本 mail id>
STATUS: done / failed (failed 时后面写 -v "错误原因")
---
"""
        return Mail(
            id=f"mail-{uuid.uuid4().hex[:12]}",
            sender=self.persona.id,
            recipients=(node.assignee,),
            subject=f"[DAG:{dag.id[:8]}] {node.title or node.id}",
            body=body,
            thread_id=f"dag-{dag.id}-{node.id}",
            in_reply_to=None,
            priority=7,
            created_at=datetime.now(),
            requires_ack=True,
        )

    # ========================================================================
    # Step 3: 检查 DAG 完成
    # ========================================================================

    async def _check_dag_completion(self):
        active = await self.dag_store.list_active_dags()
        for dag in active:
            if not dag.nodes:
                continue
            statuses = {n.status for n in dag.nodes}
            # 全 done → completed
            if statuses == {"done"} or statuses <= {"done"}:
                await self.dag_store.update_dag_status(dag.id, "completed")
                await self._notify_god(dag, "completed")
            # 任何 failed/blocked 且没有 pending/running → failed
            elif "failed" in statuses and not (statuses & {"pending", "running"}):
                await self.dag_store.update_dag_status(dag.id, "failed")
                await self._notify_god(dag, "failed")

    async def _notify_god(self, dag, outcome: str):
        """DAG 完成/失败 → 发 mail 给 god."""
        body_lines = [f"DAG {dag.id} ({dag.title}) {outcome}."]
        for n in dag.nodes:
            body_lines.append(
                f"  - {n.id:15s} {n.status:8s} assignee={n.assignee:20s} {n.title}"
            )
        if outcome == "failed":
            body_lines.append("\n失败节点:")
            for n in dag.nodes:
                if n.status in ("failed", "blocked"):
                    body_lines.append(f"  - {n.id}: {n.error or '(no error message)'}")
        mail = Mail(
            id=f"mail-{uuid.uuid4().hex[:12]}",
            sender=self.persona.id,
            recipients=("god",),
            subject=f"[DAG {outcome}] {dag.title or dag.id}",
            body="\n".join(body_lines),
            thread_id=f"dag-{dag.id}-report",
            priority=8,
            created_at=datetime.now(),
            requires_ack=False,
        )
        try:
            await self.mailbox.deliver(mail)
            if self.monitor:
                self.monitor.mail_sent(mail, by_author=self.persona.id)
        except Exception as e:
            print(f"[orchestrator] notify_god failed: {e}")

    # ========================================================================
    # Snapshot override (加 DAG 统计)
    # ========================================================================

    def snapshot(self) -> dict[str, Any]:
        s = super().snapshot()
        s["kind"] = "orchestrator"
        s["dispatches_this_tick"] = self.dispatches_this_tick
        s["reports_handled"] = self.reports_handled
        return s
