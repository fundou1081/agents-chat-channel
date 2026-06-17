# Workflow Examples — 3 个真实场景

设计文档: [docs/26-stage-workflow.md](26-stage-workflow.md) §15.1 §16

本文档展示 3 个真实使用场景, 每个都附完整 YAML + 预期 deliverable + 注意事项.

---

## 场景 1: 研报生成 (research → write → review)

**业务场景**: 自动收集行业资料, 生成结构化研报.

**YAML**: `examples/fixtures/research-pipeline.yaml`

```yaml
name: research-pipeline
stages:
  - id: research     # 收集资料
  - id: write        # 写初稿 + 审阅
    depends_on: [research]
  - id: publish      # 格式化 + 加结论
    depends_on: [write]
```

**关键设计**:
- Stage `write` 用 2 个 worker (writer + reviewer) 在同一 channel 协作
- Stage `publish` 的 checks 用 `contains_all` 确保必填 section
- 所有 deliverable 含 `min_size` 避免空文件

**预期输出**:
- `out/research.json`: 结构化资料 (findings / sources)
- `out/report.md`: 草稿报告
- `out/final.md`: 最终报告 (含 ## 结论)

**运行**:
```bash
python -m agents_chat workflow run examples/fixtures/research-pipeline.yaml
```

---

## 场景 2: 代码审计 (analyze → review → fix)

**业务场景**: 扫描代码, 发现问题, 生成修复方案.

**YAML 示例**:
```yaml
name: code-audit
stages:
  - id: analyze
    workers:
      - id: scanner
        cli: opencode
        system_prompt: |
          扫描 {input.repo_path}, 找潜在的 bug / 安全漏洞.
          输出 JSON: {findings: [{file, line, severity, issue, suggested_fix}]}
    deliverable:
      path: out/scan-result.json
      min_size: 50
      checks:
        - '"findings"'
        - type: min_keywords
          keywords: ["file", "line", "severity"]
          min_count: 2
      schema:
        type: object
        required: [findings]
    timeout: 900

  - id: review
    depends_on: [analyze]
    workers:
      - id: reviewer
        cli: opencode
        system_prompt: |
          基于 {input.findings} 评估每个 issue 的严重性, 给出修复优先级.
    deliverable:
      path: out/audit-report.md
      min_size: 200
      checks:
        - "## Critical Issues"
        - "## Recommended Fixes"
    timeout: 600

  - id: fix
    depends_on: [review]
    workers:
      - id: fixer
        cli: opencode
        system_prompt: |
          应用 {input.fix} 修复, 写补丁.
    deliverable:
      path: out/patches.json
      min_size: 50
      checks:
        - '"patches"'
    timeout: 900
```

**关键设计**:
- Stage `analyze` 跑代码扫描 (1-15 min 真实 worker)
- Stage `fix` 写补丁 JSON, 可被后续 stage 自动化应用

**适用**:
- 大代码库定期审计
- PR 自动 review

---

## 场景 3: 日报生成 (collect → format → distribute)

**业务场景**: 每天从多个数据源收集, 生成统一日报.

**YAML 示例**:
```yaml
name: daily-report
description: "每日数据收集 + 报告生成"
stages:
  - id: collect
    workers:
      - id: git-collector
        cli: mock
        system_prompt: "收集昨日 git commits"
      - id: metrics-collector
        cli: mock
        system_prompt: "收集昨日 metrics"
      - id: incident-collector
        cli: mock
        system_prompt: "收集昨日 incidents"
    deliverable:
      paths:
        - out/git-data.json
        - out/metrics.json
        - out/incidents.json
      min_size: 50
    timeout: 300

  - id: format
    depends_on: [collect]
    workers:
      - id: formatter
        cli: opencode
        system_prompt: |
          基于 3 个上游 input (git / metrics / incidents) 生成 Markdown 报告.
    deliverable:
      path: out/daily-report.md
      min_size: 500
      checks:
        - "## Git Activity"
        - "## Metrics"
        - "## Incidents"
    timeout: 300

  - id: distribute
    depends_on: [format]
    workers:
      - id: distributer
        cli: opencode
        system_prompt: "把报告发到 Slack / Email"
    deliverable:
      path: out/distribution-log.json
      min_size: 20
      checks:
        - '"sent_to"'
    timeout: 120
```

**关键设计**:
- Stage `collect` 3 个并行 worker, 每个独立产 data
- `deliverable.paths` (复数) 同时校验 3 个文件
- 适合 cron 触发 (Phase 2 功能)

**适用**:
- 工程日报
- 周报 / 月报
- 实时监控报告

---

## 通用模板

### 最小 2-stage pipeline
```yaml
name: minimal
stages:
  - id: a
    workers: [{id: w-a, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
  - id: b
    depends_on: [a]
    workers: [{id: w-b, cli: mock}]
    deliverable: {path: out/b.json, min_size: 1}
```

### 4 stage 菱形 DAG
```yaml
name: diamond
stages:
  - id: a
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/a.json, min_size: 1}
  - id: b
    depends_on: [a]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/b.json, min_size: 1}
  - id: c
    depends_on: [a]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/c.json, min_size: 1}
  - id: d
    depends_on: [b, c]
    workers: [{id: w, cli: mock}]
    deliverable: {path: out/d.json, min_size: 1}
```

---

## 最佳实践

1. **永远用 `min_size`**: 避免空文件被当成"成功"
2. **stage 内多 worker**: 用 1 个 channel 让多 worker 协调
3. **checks 写宽松**: `contains` 比 `regex` 容错
4. **timeout 留宽**: 默认 600s, 复杂 stage 1800s+
5. **deliverable 路径稳定**: 别用 `out/random.json`, 用语义化路径
6. **依赖关系明确**: 别让 `depends_on` 链超过 4 级
7. **下游 stage 引用上游 key**: `{input.findings}`, `data_dir` 阶段输入

---

## 调试

```bash
# 验证 YAML
python -m agents_chat workflow validate pipeline.yaml

# 跑 workflow
python -m agents_chat workflow run pipeline.yaml --data-dir /tmp/wf

# 看 HTML 可视化
python -m agents_chat workflow visualize pipeline.yaml --run-id run-abc12345 -o report.html
open report.html

# 看 run 历史
python -m agents_chat workflow list-runs --data-dir /tmp/wf

# 看具体 run
python -m agents_chat workflow status run-abc12345 --data-dir /tmp/wf

# 取消 running run (需要 server 启)
python -m agents_chat workflow active --server-url http://127.0.0.1:8765
python -m agents_chat workflow cancel run-abc12345 --server-url http://127.0.0.1:8765
```
