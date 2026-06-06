#!/bin/bash
# E2E test for workspace_dir pattern
#
# 验证每个 agent 自动获得独立 workspace + <cli_name>.md 引导文件

set +e
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_workspace_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E workspace pattern (data_dir=$DATA_DIR)"
echo "============================================"

# reset
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

# init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# 启动 run-all
echo ""
echo "T=2  start run-all (2 agents, mock CLI)"
$VENV -m agents_chat.v2.main run-all --data-dir "$DATA_DIR" \
    --agents qwencode claude --cli mock > /tmp/e2e_workspace_runall.log 2>&1 &
RUN_PID=$!
sleep 3

# post task
echo ""
echo "T=5  post [TASK]"
$VENV -m agents_chat.v2.main post general \
    "[TASK task_ws_001] 验证 workspace 引导" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 3

# stop
echo ""
echo "T=8  stop run-all"
kill -INT $RUN_PID 2>/dev/null
sleep 2

# 验证 workspace 目录
echo ""
echo "============================================"
echo "  验证 workspaces/ 自动生成"
echo "============================================"

echo ""
echo "=== workspace 目录树 ==="
ls -la "$DATA_DIR/workspaces/"

echo ""
echo "=== qwencode/ ==="
ls -la "$DATA_DIR/workspaces/qwencode/"

echo ""
echo "=== qwencode/mock.md (CLI 引导文件) ==="
cat "$DATA_DIR/workspaces/qwencode/mock.md" | head -30

echo ""
echo "=== claude/ ==="
ls -la "$DATA_DIR/workspaces/claude/"

echo ""
echo "=== claude/mock.md 头部 ==="
head -10 "$DATA_DIR/workspaces/claude/mock.md"

# 验证不同的 agent_id → 不同的 workspace
echo ""
echo "============================================"
echo "  验证 1: 不同 agent 有独立 workspace"
echo "============================================"
QWSHA=$(sha256sum "$DATA_DIR/workspaces/qwencode/mock.md" | awk '{print $1}')
CWSHA=$(sha256sum "$DATA_DIR/workspaces/claude/mock.md" | awk '{print $1}')
if [ "$QWSHA" != "$CWSHA" ]; then
    echo "  ✓ qwencode 和 claude 的 mock.md 不同 (sha256: $QWSHA / $CWSHA)"
else
    echo "  ✗ 错误: 应该是不同内容"
fi

# 验证 system_prompt 注入
echo ""
echo "============================================"
echo "  验证 2: system_prompt 注入 MD"
echo "============================================"
if grep -q "qwencode" "$DATA_DIR/workspaces/qwencode/mock.md"; then
    echo "  ✓ qwencode.md 含 agent_id"
fi
if grep -q "claude" "$DATA_DIR/workspaces/claude/mock.md"; then
    echo "  ✓ claude.md 含 agent_id"
fi

# 验证 STATUS 块规则
echo ""
echo "============================================"
echo "  验证 3: MD 文件含 STATUS 块规则"
echo "============================================"
if grep -q "STATUS" "$DATA_DIR/workspaces/qwencode/mock.md"; then
    echo "  ✓ mock.md 含 STATUS 块规则"
fi
if grep -q "channel" "$DATA_DIR/workspaces/qwencode/mock.md"; then
    echo "  ✓ mock.md 含频道路径说明"
fi

# 手动改 MD 看是否被覆盖
echo ""
echo "============================================"
echo "  验证 4: 已有 MD 不被 Agent 覆盖"
echo "============================================"
ORIGINAL=$(cat "$DATA_DIR/workspaces/qwencode/mock.md")
echo "# 我手动加的内容 $(date)" >> "$DATA_DIR/workspaces/qwencode/mock.md"
MANUAL=$(cat "$DATA_DIR/workspaces/qwencode/mock.md")
$VENV -m agents_chat.v2.main run-agent qwencode --cli mock --data-dir "$DATA_DIR" > /tmp/e2e_workspace_re.log 2>&1 &
RP=$!
sleep 2
kill -INT $RP 2>/dev/null
sleep 1
AFTER=$(cat "$DATA_DIR/workspaces/qwencode/mock.md")
if [ "$MANUAL" = "$AFTER" ]; then
    echo "  ✓ 手动改的 MD 保留 (Agent 不覆盖)"
else
    echo "  ✗ 错误: Agent 覆盖了手动改的 MD"
fi

echo ""
echo "============================================"
echo "  E2E workspace pattern test complete"
echo "============================================"
