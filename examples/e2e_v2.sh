#!/bin/bash
# E2E test for v2.0 (file bus + scanner + agent + scheduler)
#
# 流程:
#   1. init data dir
#   2. 启动 run-all (scanner + 2 agent + scheduler)
#   3. god 发 [TASK] (广播)
#   4. god 发 @mention
#   5. 等几个 tick, 看 reply
#   6. 检查 state_board / locks / sessions

set +e  # 不 errexit, 允许中间失败
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E v2.0 test (data_dir=$DATA_DIR)"
echo "============================================"

# 1. reset
echo ""
echo "T=0  reset data dir"
rm -rf "$DATA_DIR"

# 2. init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -3

# 3. start run-all in background
echo ""
echo "T=2  start run-all (scanner + 2 agents + scheduler)"
$VENV -m agents_chat.v2.main run-all --data-dir "$DATA_DIR" \
    --agents qwencode claude --cli mock > /tmp/e2e_v2_runall.log 2>&1 &
RUN_PID=$!
echo "  pid=$RUN_PID"
sleep 4  # 等 author 起来

# 4. post [TASK]
echo ""
echo "T=6  god 发 [TASK] 广播"
$VENV -m agents_chat.v2.main post general \
    "[TASK task_e2e_001] 写一个 hello.py 返回 'Hello World'" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 3

# 5. post mention
echo ""
echo "T=9  god 发 @qwencode mention"
$VENV -m agents_chat.v2.main post general \
    "@qwencode 帮我看下 task_e2e_001 是否完成" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 4

# 6. stop run-all
echo ""
echo "T=13 stop run-all"
kill -INT $RUN_PID 2>/dev/null
sleep 2

# 7. final state
echo ""
echo "============================================"
echo "  最终状态"
echo "============================================"

echo ""
echo "=== channels/general.jsonl ==="
$VENV -m agents_chat.v2.main tail general --n 20 --data-dir "$DATA_DIR"

echo ""
echo "=== state_board ==="
$VENV -m agents_chat.v2.main status --data-dir "$DATA_DIR"

echo ""
echo "=== locks (空 = 所有 task 完成释放) ==="
ls -la "$DATA_DIR/locks/" 2>/dev/null

echo ""
echo "=== sessions/qwencode.json ==="
cat "$DATA_DIR/sessions/qwencode.json" 2>/dev/null | python3 -m json.tool 2>/dev/null | head -20

echo ""
echo "=== scanner_state.json ==="
cat "$DATA_DIR/scanner_state.json" 2>/dev/null

echo ""
echo "=== scheduler run-all log (最后 15 行) ==="
tail -15 /tmp/e2e_v2_runall.log

echo ""
echo "============================================"
echo "  E2E v2.0 test complete"
echo "============================================"
