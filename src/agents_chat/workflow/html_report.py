"""
Workflow HTML report — self-contained DAG 可视化 + stage status.

Usage:
  from agents_chat.workflow.html_report import render_workflow_html
  html = render_workflow_html(spec, result=None)  # DAG only
  html = render_workflow_html(spec, result=result)  # DAG + status

HTML 输出:
  - 内联 Mermaid.js (CDN) 渲染 DAG 图
  - Stage status 卡片 (含 checks 详情)
  - Run metadata (started/finished/failed)
  - 响应式设计 (light/dark 支持)
"""
from __future__ import annotations

import json
from typing import Optional

from .schema import StageSpec, WorkflowSpec
from .scheduler import WorkflowRunResult


# =============================================================================
# Mermaid diagram
# =============================================================================


def _render_mermaid(spec: WorkflowSpec, result: Optional[WorkflowRunResult] = None) -> str:
    """生成 Mermaid 流程图代码 (裸语法, 无 markdown fence).

    mermaid.js 找 <div class="mermaid">...</div> 元素, 不解析 ``` fence.
    """
    lines = ["graph TD"]

    # 给每个 stage 一个 node
    state_map = {}
    if result and result.stage_states:
        state_map = result.stage_states

    for stage in spec.topological_order():
        sid = stage.id
        worker_count = len(stage.workers)
        timeout = stage.timeout

        # 状态图标
        state = state_map.get(sid, "pending")
        state_icon = {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(state, "⏳")

        # Node label
        label = f"{state_icon} {sid}\\n{worker_count} worker(s)\\n{timeout}s"
        lines.append(f'    {sid}["{label}"]')

        # 对 failed 节点加红
        if state == "failed":
            lines.append(f"    style {sid} fill:#faa,stroke:#900,color:#333")

    # Edges
    for stage in spec.stages:
        for dep in stage.depends_on:
            lines.append(f"    {dep} --> {stage.id}")

    # mermaid.js 需要 <div class="mermaid"> 包装
    body = "\n".join(lines)
    return f'<div class="mermaid">\n{body}\n</div>'


# =============================================================================
# Stage cards
# =============================================================================


def _render_stage_cards(
    spec: WorkflowSpec,
    result: Optional[WorkflowRunResult] = None,
) -> str:
    """生成 stage status 卡片 HTML."""
    parts = ['<div class="stage-cards">']
    
    for stage in spec.topological_order():
        sid = stage.id
        state = "pending"
        if result and result.stage_states:
            state = result.stage_states.get(sid, "pending")

        state_class = {
            "success": "state-success",
            "failed": "state-failed",
            "running": "state-running",
            "pending": "state-pending",
        }.get(state, "state-pending")

        state_icon = {"success": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(state, "⏳")

        # Worker list
        worker_lines = ""
        for w in stage.workers:
            model_str = f" (model={w.model})" if w.model else ""
            worker_lines += f'        <li>{w.id} / {w.cli}{model_str}</li>\n'

        # Checks result
        checks_html = ""
        if result and sid in result.check_results:
            cr = result.check_results[sid]
            passed_icon = "✅" if cr.all_passed else "❌"
            checks_html += f'    <div class="checks {("checks-ok" if cr.all_passed else "checks-fail")}">\n'
            checks_html += f'      <strong>{passed_icon} Checks ({len(cr.items)} items)</strong>\n'
            for item in cr.items:
                ipass = "✅" if item.passed else "❌"
                checks_html += f'      <div class="check-item"> {ipass} <code>{item.type}</code>: {item.detail}</div>\n'
            checks_html += "    </div>\n"

        # Dependencies
        dep_str = ""
        if stage.depends_on:
            dep_str = f" (depends: {', '.join(stage.depends_on)})"

        d = stage.deliverable
        deliverable_str = d.path or (d.paths[0] if d.paths else d.dir or "(none)")

        parts.append(f"""
    <div class="stage-card {state_class}">
      <h3>{state_icon} {sid}{dep_str}</h3>
      <div class="card-body">
        <div class="card-row">
          <span class="label">Workers:</span>
          <ul>{worker_lines}        </ul>
        </div>
        <div class="card-row">
          <span class="label">Deliverable:</span>
          <code>{deliverable_str}</code> (min={d.min_size}, max={d.max_size or '∞'})
        </div>
        <div class="card-row">
          <span class="label">Timeout:</span>
          <span>{stage.timeout}s</span>
        </div>
{checks_html}
      </div>
    </div>""")

    parts.append("</div>")
    return "\n".join(parts)


# =============================================================================
# Full HTML page
# =============================================================================


def render_workflow_html(
    spec: WorkflowSpec,
    result: Optional[WorkflowRunResult] = None,
    title: str = "",
) -> str:
    """生成完整 HTML 页面 (自包含, 内联 CSS + Mermaid CDN).

    Args:
        spec: WorkflowSpec (已 load + 验证)
        result: WorkflowRunResult (可选, 有则显示 status)
        title: 页面标题 (默认用 workflow name)
    """
    wf_name = title or spec.name
    run_info = ""
    if result:
        status_emoji = {"success": "✅", "failed": "❌", "running": "🔄"}.get(result.status, "")
        run_info = f"""
    <div class="run-meta">
      <h2>{status_emoji} Run: {result.run_id}</h2>
      <p>Status: <strong>{result.status}</strong> | Started: {result.started_at[:19]} | Finished: {(result.finished_at or '?')[:19]}</p>
      {"<p class='failed-stage'>❌ Failed at: " + result.failed_stage + "</p>" if result.failed_stage else ""}
    </div>"""

    mermaid = _render_mermaid(spec, result)
    stage_cards = _render_stage_cards(spec, result)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{wf_name} — Workflow</title>
<script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; padding: 20px; }}
.container {{ max-width: 1200px; margin: 0 auto; }}
h1 {{ font-size: 1.8em; margin-bottom: 8px; }}
.run-meta {{ background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.run-meta h2 {{ font-size: 1.2em; }}
.run-meta p {{ margin-top: 4px; color: #666; }}
.failed-stage {{ color: #c00; font-weight: bold; }}

/* Mermaid */
.mermaid-container {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.mermaid-container h2 {{ margin-bottom: 16px; }}

/* Stage Cards */
.stage-cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 16px; }}
.stage-card {{ background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-left: 4px solid #aaa; }}
.stage-card h3 {{ font-size: 1.1em; margin-bottom: 8px; }}
.state-success {{ border-left-color: #2ecc40; }}
.state-failed {{ border-left-color: #ff4136; }}
.state-running {{ border-left-color: #0074d9; }}
.state-pending {{ border-left-color: #aaa; }}
.card-body {{ font-size: 0.9em; }}
.card-row {{ margin-bottom: 6px; }}
.card-row .label {{ font-weight: 600; display: inline-block; min-width: 100px; }}
.card-row ul {{ list-style: none; display: inline; }}
.card-row li {{ display: inline; margin-right: 8px; }}
.card-row code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 3px; font-size: 0.85em; }}
.checks {{ margin-top: 8px; padding: 8px; border-radius: 4px; font-size: 0.85em; }}
.checks-ok {{ background: #e8f5e9; }}
.checks-fail {{ background: #fce4ec; }}
.checks strong {{ display: block; margin-bottom: 4px; }}
.check-item {{ margin-top: 2px; }}

/* Responsive */
@media (max-width: 768px) {{
  .stage-cards {{ grid-template-columns: 1fr; }}
  body {{ padding: 10px; }}
}}

/* Dark mode */
@media (prefers-color-scheme: dark) {{
  body {{ background: #1a1a2e; color: #ddd; }}
  .mermaid-container, .stage-card, .run-meta {{ background: #16213e; color: #ddd; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }}
  .card-row code {{ background: #0f3460; }}
  .run-meta p {{ color: #aaa; }}
  .checks-ok {{ background: #1b4332; }}
  .checks-fail {{ background: #4a0e0e; }}
}}
</style>
</head>
<body>
<div class="container">
  <h1>📋 {wf_name}</h1>
  <p style="color:#888;margin-bottom:20px">{spec.description or 'Multi-stage pipeline workflow'}</p>
{run_info}
  <div class="mermaid-container">
    <h2>🔀 DAG 依赖图</h2>
{mermaid}
  </div>
  <h2 style="margin-bottom:12px">📊 Stage 详情</h2>
{stage_cards}
</div>
<script>
mermaid.initialize({{ startOnLoad: true, theme: 'base', securityLevel: 'loose' }});
</script>
</body>
</html>"""
    return html


# =============================================================================
# Visualize CLI handler
# =============================================================================


def render_and_save_html(
    spec: WorkflowSpec,
    output_path: str,
    result: Optional[WorkflowRunResult] = None,
) -> None:
    """渲染 HTML 并保存到文件."""
    html = render_workflow_html(spec, result=result)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Saved to {output_path} ({len(html)} bytes)")
