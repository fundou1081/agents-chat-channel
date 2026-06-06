# 12. Workspace Pattern — Per-Agent 独立工作目录 + <cli_name>.md 引导

> 状态: ✅ 已实施 (commit f382458)
> 动机: opencode / qwen / claude 等 CLI 工具对 `--system-prompt` API 不友好, 但会读**工作目录**里专属的 MD 引导文件 (claude.md / opencode.md / qwen.md).
> 解决: 每个 agent 一个独立 workspace, 启动时写 `<cli_name>.md` 角色定义.

## 背景

`docs/11-v2-architecture.md` 第 8 节提到 "每个 Agent 绑一个外部智能体 CLI 程序 (qwen/claude/opencode)", 实际部署时发现:

| 工具 | 引导方式 | 支持度 |
|------|---------|--------|
| `claude` CLI | 读 `claude.md` (或 `CLAUDE.md`) | ✅ 推荐 |
| `opencode` CLI | 读 `opencode.md` (或 `AGENTS.md`) | ✅ 推荐 |
| `qwen` CLI / API | 读 `qwen.md` (作为 system prompt 补充) | ⚠️ 部分 |
| `codex` CLI | 读 `codex.md` | ✅ 推荐 |

**经验**: 通过 `--system-prompt "..."` 传长 system prompt 不可靠 (部分 CLI 截断 / 忽略).
**解决**: 把角色定义写到工作目录的 `<cli_name>.md` 文件, CLI 启动时自动加载.

## 设计

### Agent 构造加 `workspace_dir`

```python
class Agent:
    def __init__(
        self,
        agent_id: str,
        cli: CLI,
        data_dir: str | Path,
        workspace_dir: str | Path | None = None,  # 新增
        system_prompt: str = "",                    # 注入到 <cli_name>.md
        ...
    ):
        # 默认 workspace_dir = data_dir/workspaces/{agent_id}
        if workspace_dir is None:
            workspace_dir = self.data_dir / "workspaces" / agent_id
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        # 启动时写 {cli.name}.md 引导文件
        self._init_workspace_files()
```

### 启动时写 `<cli_name>.md`

```python
def _init_workspace_files(self):
    md_name = f"{self.cli.name}.md"  # claude.md / opencode.md / qwen.md
    md_path = self.workspace_dir / md_name
    if md_path.exists():
        return  # 不覆盖, 用户可能手动改了
    md_path.write_text(self._build_workspace_md(), encoding="utf-8")
```

### CLI 启动时 `cwd=workspace_dir`

```python
# OpenCodeCLI.invoke
async def invoke(self, prompt, resume_session=None, workspace_dir=None):
    cmd = [self.binary, "run", prompt]
    if resume_session:
        cmd.extend(["--session", resume_session])
    kwargs = {}
    if workspace_dir:
        kwargs["cwd"] = workspace_dir  # ← 关键
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=PIPE, stderr=PIPE, **kwargs,
    )
    ...
```

### QwenCLI 的 HTTP API workaround

`qwen` 用 OpenAI-compatible HTTP API, **不能** `cwd`. 但可以在 prompt 里 prefix `qwen.md` 内容:

```python
# QwenCLI.invoke
async def invoke(self, prompt, resume_session=None, workspace_dir=None):
    if workspace_dir:
        md_path = Path(workspace_dir) / "qwen.md"
        if md_path.exists():
            md_content = md_path.read_text("utf-8")[:2000]
            prompt = f"[Workspace Guide: read {md_path}]\n{md_content}\n\n[Task]\n{prompt}"
    # 然后正常调 API
```

## <cli_name>.md 内容模板

```markdown
# {agent_id} 角色定义 (v2.0)

你是 **{agent_id}**, 在多 agent 协作网络中工作. 本文件由 Agent 启动时自动生成,
你可以手动修改, Agent 不会覆盖.

## 能力标签

{capabilities 或 "通用"}

## 角色提示 (system_prompt)

{system_prompt 或 "(无)"}

## 工作环境 (相对本文件)

- 频道: `../channels/{name}.jsonl` (JSONL, 可读可写)
- 我的邮箱: `../mailboxes/{agent_id}.json` (pending 邮件)
- 我的 session 索引: `../sessions/{agent_id}.json` (local → remote 映射)
- 任务状态板: `../state_board.json` (全局, 只读)
- 任务锁: `../locks/task_xxx.lock` (5min TTL, mtime 判超时)

## 工作规则

1. **STATUS 块**: 每条 reply 必须嵌入, Scanner 解析用于调度
2. **@mention**: reply 里的 `@xxx` 由 Scanner 自动投递 mention 邮件给 xxx
3. **[TASK] 标记**: 频道消息里的 `[TASK task_xxx]` 会被 Scanner 广播成 task_broadcast
4. **session resume**: 同一 task 多次处理会自动用同一 session_id (本地映射)

## 本文件说明

- 文件名: `{cli.name}.md` (跟 CLI 工具名匹配)
- CLI ({cli.name}) 启动后会自动读本文件作为角色引导
- 修改本文件不会影响 Agent 行为 (Agent 不读), 但会影响 CLI 行为
```

## 实际运行效果

`run-all` 启动后:
```
data_v2/
└── workspaces/
    ├── qwencode/
    │   ├── mock.md    # MockCLI 测试用
    │   └── (用户可手动改的临时文件 / 任务中间结果)
    └── claude/
        ├── mock.md
        └── ...
```

切换到真实 CLI (e.g. `--cli opencode`):
```
data_v2/
└── workspaces/
    ├── qwencode/
    │   └── opencode.md   # opencode CLI 读这个
    └── claude/
        └── opencode.md
```

## CLI 调用

```bash
# 默认 workspace_dir (data_dir/workspaces/{agent_id})
python -m agents_chat.v2.main run-agent qwencode --cli qwen --data-dir ./data_v2

# 自定义 workspace_dir
python -m agents_chat.v2.main run-agent qwencode --cli opencode \
    --workspace-dir /Users/me/projects/qwencode-ws \
    --data-dir ./data_v2

# 传 system_prompt (写到 <cli>.md)
python -m agents_chat.v2.main run-agent qwencode --cli opencode \
    --system-prompt "你是 PostgreSQL 专家, 专注查询优化" \
    --data-dir ./data_v2

# 自定义 <cli_name>.md (agent 不会覆盖)
# 编辑 data_v2/workspaces/qwencode/opencode.md 后, 重启 agent, 内容保留
```

## 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| workspace_dir 命名 | `data_dir/workspaces/{agent_id}` | 跟 data_v2/ 平级, 跟 channels/mailboxes 平行 |
| MD 文件名 | `{cli.name}.md` (e.g. `opencode.md`) | 跟 CLI 工具名一致, 工具自己认 |
| 已存在 MD 处理 | 保留, 不覆盖 | 用户可能手动改, 避免丢失 |
| QwenCLI workaround | prompt prefix | HTTP API 不能 cwd, 但可注入 |
| 相对路径 | MD 里用 `../channels/...` 相对路径 | agent 可以自己读, 不需要知道 data_dir 绝对路径 |

## 经验总结 (跟 v2.0 设计文档 8 节对比)

| 维度 | 原设计 | 改进后 |
|------|--------|--------|
| Agent 工作目录 | 共享一个 (从 env var) | **每 agent 独立** |
| 角色引导 | system prompt 参数 | **`<cli_name>.md` 文件** |
| 适用 CLI | 假设 CLI 都有 `--system-prompt` | **所有 CLI 都支持工作目录 md** |
| 可调试性 | 不容易改 system prompt | **直接编辑 md 文件** |
| 跨 session 一致性 | system prompt 每次传 | **md 文件保持不变** |

## 验证

测试: 10 个新 unit tests 覆盖 workspace_dir 功能 (tests/unit/v2/test_workspace.py)
- 默认/自定义 workspace_dir 路径
- 自动写 <cli_name>.md
- 不同 CLI 名字 → 不同 MD 文件
- 已存在 MD 不覆盖
- capabilities / system_prompt 注入 MD
- QwenCLI prompt prefix qwen.md 内容
- OpenCodeCLI subprocess cwd=workspace_dir

E2E: `examples/e2e_workspace.sh` (待写) — 跑 run-all 后看 `workspaces/{agent}/{cli}.md` 自动生成
