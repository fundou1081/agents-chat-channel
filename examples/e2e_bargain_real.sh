#!/bin/bash
# E2E 卖鱼/买鱼 — god 主动控制节奏 (passive 模式 + OpenCodeCLI)
#
# 跟 e2e_bargain_new.sh (agent 自主 / proactive) 的区别:
#   - 这里 god 持续 @sell/@buy 邮件, agent 等 @mention 触发
#   - agent 不会主动发消息, 完全被动
#   - 适合测试 god 当导演的人机混合场景
#
# 用法:
#   MAX_ROUNDS=6 TIMEOUT_SECS=180 bash e2e_bargain_real.sh
#
# 参数:
#   MAX_ROUNDS   god 发邮件轮数 (默认 6, 即 3 轮来回). 达到后主动 stop.
#   TIMEOUT_SECS 全局超时 (默认 180s). 到达后 kill -9 整个进程树.

set +e
cd "$(dirname "$0")/.."

# =============================================================================
# 参数
# =============================================================================
MAX_ROUNDS="${MAX_ROUNDS:-6}"        # god 发 6 封邮件 (3 轮 seller↔buyer 来回)
TIMEOUT_SECS="${TIMEOUT_SECS:-180}"  # 3 分钟全局超时
OPENCODE_WAIT="${OPENCODE_WAIT:-25}" # 每次 god 发邮件后等待 opencode 处理
DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_real_e2e}"
VENV="${VENV:-.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/e2e_bargain_real.log}"

echo "============================================"
echo "  E2E 卖鱼/买鱼 (god 控制节奏 / OpenCodeCLI)"
echo "  MAX_ROUNDS=$MAX_ROUNDS  TIMEOUT_SECS=${TIMEOUT_SECS}s  WAIT=${OPENCODE_WAIT}s"
echo "============================================"

# =============================================================================
# 清理函数
# =============================================================================
KILL_PIDS=""
cleanup() {
    echo ""
    echo "[cleanup] ⏹ stopping..."
    for pid in $KILL_PIDS; do
        kill -INT $pid 2>/dev/null || true
    done
    sleep 2
    pkill -9 -f "opencode" 2>/dev/null || true
    pkill -9 -f "agents_chat" 2>/dev/null || true
    echo "[cleanup] done"
    exit 0
}
trap cleanup INT TERM

# =============================================================================
# T=1: init
# =============================================================================
echo ""
echo "T=1  init"
$VENV -m agents_chat.main --data-dir "$DATA_DIR" init 2>&1 | tail -2

# =============================================================================
# 准备 workspace + role.md (简短, 减少 token)
# =============================================================================
WS1="$DATA_DIR/workspaces/seller-fish"
WS2="$DATA_DIR/workspaces/buyer-fish"
mkdir -p "$WS1" "$WS2"

cat > "$WS1/role.md" << 'EOF'
# seller-fish 角色 (卖鱼小贩)
在 fish-market 频道跟 buyer-fish 讨价还价.
策略: 开价 100, 最低 80, 理想成交价 90.
回复格式: 简短报价 + STATUS 块:
<!--STATUS
 session_id: seller-sess
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 buyer / 成交>
 confidence: high
-->
EOF

cat > "$WS2/role.md" << 'EOF'
# buyer-fish 角色 (买鱼顾客)
在 fish-market 频道跟 seller-fish 讨价还价.
策略: 预算 90, 起步还价 70, 90 以内接受.
回复格式: 简短还价或接受 + STATUS 块:
<!--STATUS
 session_id: buyer-sess
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 seller / 成交>
 confidence: high
-->
EOF

echo "  ✓ 2 个 workspace + role.md"

# =============================================================================
# T=2: 配置 fish-market 频道 (god=admin, 白名单 seller/buyer)
# =============================================================================
echo ""
echo "T=2  配置 fish-market 频道成员"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market', max_messages=20)
ch.add_admin('god')              # god 是频道管理员
ch.add_member('seller-fish')
ch.add_member('buyer-fish')
ch.add_member('admin')
# 白名单: 只允许 seller-fish 和 buyer-fish 响应
ch.set_enabled_workers(['seller-fish', 'buyer-fish'])
print('  admin:', ch.list_admins())
print('  members:', ch.list_members())
print('  enabled_workers:', ch.list_enabled_workers())
print('  max_messages:', ch.max_messages)
"

# =============================================================================
# T=3: 启动 2 个 agent (passive 模式, god 主动 @mention 触发)
# =============================================================================
echo ""
echo "T=3  启动 2 个 agent (passive 模式, OpenCodeCLI, god 控制节奏)"

MAX_ROUNDS_VAL=$MAX_ROUNDS
TIMEOUT_SECS_VAL=$TIMEOUT_SECS

$VENV -c "
import asyncio, sys, time
sys.path.insert(0, 'src')
from agents_chat.core.agent import Agent
from agents_chat.infra.cli import OpenCodeCLI
from agents_chat.infra.files import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')
MAX_ROUNDS = $MAX_ROUNDS_VAL
TIMEOUT_SECS = $TIMEOUT_SECS_VAL

# init mailboxes
for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# CLI: timeout 保护 (单次 LLM call 超时 2 分钟)
cli = OpenCodeCLI(timeout_seconds=120)

# Agent: passive 模式 (god 主动 @mention 触发)
agent_seller = Agent(
    agent_id='seller-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'seller-fish',
    poll_interval=1.0, default_channel='fish-market',
    mode='passive',  # 等 god @mention 触发
)
agent_buyer = Agent(
    agent_id='buyer-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'buyer-fish',
    poll_interval=1.0, default_channel='fish-market',
    mode='passive',
)

start_time = time.time()
_run_tasks = []

async def guard():
    '''超时到达时 cancel 所有 run tasks.'''
    _timeout = TIMEOUT_SECS if TIMEOUT_SECS else 300
    while True:
        await asyncio.sleep(2)
        elapsed = time.time() - start_time
        if elapsed >= _timeout:
            print(f'[guard] timeout ({_timeout}s), cancelling all tasks...')
            for t in _run_tasks:
                if not t.done():
                    t.cancel()
            import subprocess
            subprocess.run(['pkill', '-9', '-f', 'opencode'], capture_output=True)
            agent_seller.stop()
            agent_buyer.stop()
            break

async def main():
    global _run_tasks
    _run_tasks = [
        asyncio.create_task(agent_seller.run()),
        asyncio.create_task(agent_buyer.run()),
        asyncio.create_task(guard()),
    ]
    print(f'[run-all] started: 2 agents (passive), MAX_ROUNDS={MAX_ROUNDS}, TIMEOUT={TIMEOUT_SECS}s')
    try:
        await asyncio.gather(*_run_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        print(f'[run-all] interrupted: {type(e).__name__}')
        for t in _run_tasks:
            if not t.done():
                t.cancel()
        agent_seller.stop()
        agent_buyer.stop()
    finally:
        print('[run-all] stopped')

asyncio.run(main())
" > "$LOG_FILE" 2>&1 &
RUN_PID=$!
KILL_PIDS="$RUN_PID"
echo "  run-all pid=$RUN_PID, log=$LOG_FILE"

# 等待 agents 启动
sleep 8

# =============================================================================
# god 控制节奏: 持续发邮件, 每封间隔 OPENCODE_WAIT 秒
# =============================================================================
POST="$VENV -m agents_chat.main --data-dir $DATA_DIR post fish-market --from god"

echo ""
echo "T=11  Round 0: god @sell 开价"
$POST "@sell 你好, 卖鱼吗? 开个价" 2>&1 | tail -1
sleep $OPENCODE_WAIT

# Round 1-N: 持续 god 主动控制
if [ "${MAX_ROUNDS:-0}" -ge 1 ]; then
    echo ""
    echo "T=11+$OPENCODE_WAIT  Round 1: god @buy 还价 70"
    $POST "@buy seller 报 100 太贵, 70 卖不卖?" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if [ "${MAX_ROUNDS:-0}" -ge 2 ]; then
    echo ""
    echo "T=11+2*$OPENCODE_WAIT  Round 2: god @sell 接受 80"
    $POST "@sell buyer 出 70 太少, 80 行不行?" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if [ "${MAX_ROUNDS:-0}" -ge 3 ]; then
    echo ""
    echo "T=11+3*$OPENCODE_WAIT  Round 3: god @buy 接受 80"
    $POST "@buy seller 80 元, 成交!" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if [ "${MAX_ROUNDS:-0}" -ge 4 ]; then
    echo ""
    echo "T=11+4*$OPENCODE_WAIT  Round 4: god @sell 确认"
    $POST "@sell 成交! 明天来取鱼" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if [ "${MAX_ROUNDS:-0}" -ge 5 ]; then
    echo ""
    echo "T=11+5*$OPENCODE_WAIT  Round 5: god @buy 确认"
    $POST "@buy 明天来取鱼, 80 元" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

# =============================================================================
# 主动 stop (达到 max rounds)
# =============================================================================
echo ""
echo "T=end  ⏹ 达到 MAX_ROUNDS=$MAX_ROUNDS, 主动 stop"
kill -INT $RUN_PID 2>/dev/null || true
echo "  waiting for run-all (pid=$RUN_PID) to exit..."
_deadline=$(($(date +%s) + 30))
while kill -0 $RUN_PID 2>/dev/null; do
    if [ $(date +%s) -gt $_deadline ]; then
        echo "  timeout, sending SIGKILL..."
        kill -9 $RUN_PID 2>/dev/null || true
        break
    fi
    sleep 1
done
echo "  run-all exited"
pkill -9 -f "opencode" 2>/dev/null || true

# =============================================================================
# 验证
# =============================================================================
echo ""
echo "============================================"
echo "  验证 fish-market 频道对话"
echo "============================================"
$VENV -m agents_chat.main --data-dir "$DATA_DIR" tail fish-market 20 2>&1 | head -30

echo ""
echo "[done] data_dir: $DATA_DIR"
echo "[done] log:      $LOG_FILE"
