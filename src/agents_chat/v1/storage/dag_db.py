"""
DagStore: DAG (有向无环图) 持久化存储 (Phase 3).

设计:
  - 2 张表: dags (DAG 元信息) + dag_nodes (节点, 跟 DAG 1-to-N)
  - status 字段用 enum 值, 由 Orchestrator 推进
  - 节点 depends_on 用 JSON 数组 (e.g. ["api", "ui"])
  - 所有写操作原子 (single transaction)

跟 Posts / Mailbox 的关系:
  - 提交 DAG 时同时建一个 Post(kind="dag_dispatch") (audit)
  - 派发 node 时发 Mail (1-to-1 DM, 1 个 node 1 封)
  - author 回执 = 回 Mail 给 orchestrator, Orchestrator 据此更新 DagStore
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from ..models import DAG, DagNode, DagNodeStatus, DagStatus


SCHEMA = """
CREATE TABLE IF NOT EXISTS dags (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- DagStatus
    post_id TEXT,                             -- 关联的 dag_dispatch post
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS dag_nodes (
    id TEXT NOT NULL,              -- DAG 内 node id (e.g. "api")
    dag_id TEXT NOT NULL,
    title TEXT,
    body TEXT,
    assignee TEXT,
    depends_on TEXT,               -- JSON list of node ids
    status TEXT NOT NULL DEFAULT 'pending',  -- DagNodeStatus
    started_at TEXT,
    completed_at TEXT,
    dispatch_mail_id TEXT,
    report_mail_id TEXT,
    error TEXT,
    timeout_at TEXT,
    PRIMARY KEY (dag_id, id),
    FOREIGN KEY (dag_id) REFERENCES dags(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_dag_nodes_dag ON dag_nodes(dag_id);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_status ON dag_nodes(dag_id, status);
CREATE INDEX IF NOT EXISTS idx_dags_status ON dags(status);
"""


class DagStore:
    """DAG + 节点 持久化."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    async def _ensure_schema(self):
        if self._initialized:
            return
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()
        self._initialized = True

    # ------------------------------------------------------------------
    # DAG CRUD
    # ------------------------------------------------------------------

    async def create_dag(self, dag: DAG) -> DAG:
        """新建 DAG. 默认 status=active (pending 是中间态, 提交即激活)."""
        await self._ensure_schema()
        if not dag.id:
            dag.id = f"dag-{uuid.uuid4().hex[:8]}"
        if not dag.created_at:
            dag.created_at = datetime.now().isoformat()
        if dag.status == "pending":
            dag.status = "active"  # submit 即激活, Orchestrator 立刻扫

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO dags (id, title, description, created_by, created_at, status, post_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (dag.id, dag.title, dag.description, dag.created_by,
                 dag.created_at, dag.status, dag.post_id),
            )
            for n in dag.nodes:
                await self._insert_node(db, dag.id, n)
            await db.commit()
        return dag

    async def get_dag(self, dag_id: str) -> DAG | None:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM dags WHERE id = ?", (dag_id,))
            row = await cur.fetchone()
            if not row:
                return None
            cur = await db.execute(
                "SELECT * FROM dag_nodes WHERE dag_id = ?", (dag_id,)
            )
            node_rows = await cur.fetchall()
        nodes = [self._row_to_node(r) for r in node_rows]
        return DAG(
            id=row["id"], title=row["title"] or "", description=row["description"] or "",
            created_by=row["created_by"], created_at=row["created_at"],
            status=row["status"], post_id=row["post_id"] or "",
            completed_at=row["completed_at"] or "", nodes=nodes,
        )

    async def list_dags(self, limit: int = 50) -> list[DAG]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id FROM dags ORDER BY created_at DESC LIMIT ?", (limit,)
            )
            ids = [r[0] for r in await cur.fetchall()]
        result = []
        for i in ids:
            d = await self.get_dag(i)
            if d:
                result.append(d)
        return result

    async def list_active_dags(self) -> list[DAG]:
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id FROM dags WHERE status = 'active' ORDER BY created_at"
            )
            ids = [r[0] for r in await cur.fetchall()]
        result = []
        for i in ids:
            d = await self.get_dag(i)
            if d:
                result.append(d)
        return result

    async def update_dag_status(self, dag_id: str, status: str, completed_at: str = "") -> bool:
        """更新 DAG 状态. status 必须是 DagStatus 之一."""
        if status not in [s.value for s in DagStatus]:
            raise ValueError(f"invalid dag status: {status}")
        await self._ensure_schema()
        if status in ("completed", "failed", "cancelled") and not completed_at:
            completed_at = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "UPDATE dags SET status = ?, completed_at = ? WHERE id = ?",
                (status, completed_at, dag_id),
            )
            await db.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Node 操作
    # ------------------------------------------------------------------

    async def update_node(self, dag_id: str, node: DagNode) -> bool:
        """更新整个 node (覆盖)."""
        await self._ensure_schema()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE dag_nodes SET
                    title = ?, body = ?, assignee = ?, depends_on = ?,
                    status = ?, started_at = ?, completed_at = ?,
                    dispatch_mail_id = ?, report_mail_id = ?,
                    error = ?, timeout_at = ?
                WHERE dag_id = ? AND id = ?
                """,
                (
                    node.title, node.body, node.assignee,
                    json.dumps(node.depends_on), node.status,
                    node.started_at, node.completed_at,
                    node.dispatch_mail_id, node.report_mail_id,
                    node.error, node.timeout_at,
                    dag_id, node.id,
                ),
            )
            await db.commit()
        return cur.rowcount > 0

    async def update_node_status(
        self, dag_id: str, node_id: str, status: str,
        started_at: str = "", completed_at: str = "",
        report_mail_id: str = "", error: str = "",
        timeout_at: str = "", dispatch_mail_id: str = "",
    ) -> bool:
        """快捷更新 node status + 关联字段. status 必须是 DagNodeStatus 之一."""
        if status not in [s.value for s in DagNodeStatus]:
            raise ValueError(f"invalid node status: {status}")
        await self._ensure_schema()
        if status == "running" and not started_at:
            started_at = datetime.now().isoformat()
        if status in ("done", "failed") and not completed_at:
            completed_at = datetime.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE dag_nodes SET
                    status = ?,
                    started_at = COALESCE(NULLIF(?, ''), started_at),
                    completed_at = COALESCE(NULLIF(?, ''), completed_at),
                    report_mail_id = COALESCE(NULLIF(?, ''), report_mail_id),
                    error = COALESCE(NULLIF(?, ''), error),
                    timeout_at = COALESCE(NULLIF(?, ''), timeout_at),
                    dispatch_mail_id = COALESCE(NULLIF(?, ''), dispatch_mail_id)
                WHERE dag_id = ? AND id = ?
                """,
                (status, started_at, completed_at, report_mail_id, error, timeout_at, dispatch_mail_id, dag_id, node_id),
            )
            await db.commit()
        return cur.rowcount > 0

    async def block_descendants(self, dag_id: str, failed_node_id: str) -> list[str]:
        """把所有依赖 failed_node 的 pending 节点标 blocked. 返回被 block 的 id 列表."""
        await self._ensure_schema()
        dag = await self.get_dag(dag_id)
        if not dag:
            return []
        blocked = []
        for n in dag.nodes:
            if n.status != "pending":
                continue
            if failed_node_id in n.depends_on:
                await self.update_node_status(dag_id, n.id, "blocked", error=f"upstream {failed_node_id} failed")
                blocked.append(n.id)
        return blocked

    # ------------------------------------------------------------------
    # 内部 helpers
    # ------------------------------------------------------------------

    async def _insert_node(self, db: aiosqlite.Connection, dag_id: str, n: DagNode):
        await db.execute(
            """
            INSERT INTO dag_nodes (id, dag_id, title, body, assignee, depends_on, status,
                                   started_at, completed_at, dispatch_mail_id, report_mail_id,
                                   error, timeout_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (n.id, dag_id, n.title, n.body, n.assignee,
             json.dumps(n.depends_on), n.status,
             n.started_at, n.completed_at,
             n.dispatch_mail_id, n.report_mail_id,
             n.error, n.timeout_at),
        )

    def _row_to_node(self, row: aiosqlite.Row) -> DagNode:
        deps = json.loads(row["depends_on"]) if row["depends_on"] else []
        return DagNode(
            id=row["id"], title=row["title"] or "", body=row["body"] or "",
            assignee=row["assignee"] or "", depends_on=deps,
            status=row["status"],
            started_at=row["started_at"] or "", completed_at=row["completed_at"] or "",
            dispatch_mail_id=row["dispatch_mail_id"] or "",
            report_mail_id=row["report_mail_id"] or "",
            error=row["error"] or "", timeout_at=row["timeout_at"] or "",
        )
