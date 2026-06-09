#!/bin/bash
# E2E 卖鱼/买鱼 (真 OpenCodeCLI + opencode/minimax-m3-free, 不同 workspace)
# + 超时保护 (max_rounds + timeout) 防止无限循环
#
# 用法:
#   MAX_ROUNDS=6 TIMEOUT_SECS=180 bash e2e_bargain_real.sh
#
# 参数:
#   MAX_ROUNDS   最大消息轮数 (默认 6, 即 3 轮来回). 达到后主动 stop.
#   TIMEOUT_SECS 全局超时 (默认 180s). 到达后 kill -9 整个进程树.

set +e
cd "$(dirname "$0")/.."

# =============================================================================
# 参数
# =============================================================================
MAX_ROUNDS="${MAX_ROUNDS:-6}"       # 6 封邮件 → 3 轮来回
TIMEOUT_SECS="${TIMEOUT_SECS:-180}"  # 3 分钟全局超时
DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_real_e2e}"
VENV="${VENV:-.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/e2e_bargain_real.log}"

echo "============================================"
echo "  E2E 讨价还价 (OpenCodeCLI + opencode/deepseek-v4-flash-free)"
echo "  MAX_ROUNDS=$MAX_ROUNDS  TIMEOUT_SECS=${TIMEOUT_SECS}s"
echo "============================================"

# =============================================================================
# 清理函数
# =============================================================================
KILL_PIDS=""
cleanup() {
    echo ""
    echo "[cleanup] ⏹ stopping run-all (max_rounds or timeout)..."
    # 优雅 stop (SIGINT → asyncio.CancelledError)
    for pid in $KILL_PIDS; do
        kill -INT $pid 2>/dev/null || true
    done
    sleep 2
    # 强制 kill (SIGKILL)
    pkill -9 -f "opencode" 2>/dev/null || true
    pkill -9 -f "agents_chat" 2>/dev/null || true
    pkill -9 -f "asyncio" 2>/dev/null || true
    echo "[cleanup] done"
}
trap cleanup EXIT INT TERM

# =============================================================================
# reset
# =============================================================================
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

# =============================================================================
# init
# =============================================================================
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# =============================================================================
# 准备 workspace + opencode.md (简短, 减少 token)
# =============================================================================
WS1="$DATA_DIR/workspaces/seller-fish"
WS2="$DATA_DIR/workspaces/buyer-fish"
mkdir -p "$WS1" "$WS2"

cat > "$WS1/opencode.md" << 'EOF'
# seller-fish 角色
你是 seller-fish (卖鱼小贩). 跟 buyer-fish 讨价还价.
策略: 开价 100, 最低 80, 理想 90.
回复格式: 简短报价 + STATUS 块:
<!--STATUS
 session_id: seller-sess
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 buyer/成交>
 confidence: high
-->
EOF

cat > "$WS2/opencode.md" << 'EOF'
# buyer-fish 角色
你是 buyer-fish (买鱼顾客). 跟 seller-fish 讨价还价.
策略: 预算 90, 起步 70, 90 就接受.
回复格式: 简短还价或接受 + STATUS 块:
<!--STATUS
 session_id: buyer-sess
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 seller/成交>
 confidence: high
-->
EOF

echo "  ✓ 2 个 workspace + opencode.md"

# =============================================================================
# 配置频道成员
# =============================================================================
echo ""
echo "T=2  配置 fish-market 频道成员"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.v2.infra.files import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
ch.add_admin('god')              # god 是频道管理员
ch.add_member('seller-fish')
ch.add_member('buyer-fish')
ch.add_member('admin')
# 白名单: 只允许 seller-fish 和 buyer-fish 响应
ch.set_enabled_workers(['seller-fish', 'buyer-fish'])
print('  admin:', ch.list_admins())
print('  members:', ch.list_members())
print('  enabled_workers:', ch.list_enabled_workers())
"

# =============================================================================
# 启动 run-all (后台)
# =============================================================================
echo ""
echo "T=3  启动 run-all (OpenCodeCLI, 2 agents, MAX_ROUNDS=$MAX_ROUNDS, TIMEOUT=${TIMEOUT_SECS}s)"

MAX_ROUNDS_VAL=$MAX_ROUNDS
TIMEOUT_SECS_VAL=$TIMEOUT_SECS
$VENV -c "
import asyncio, sys, time, signal
sys.path.insert(0, 'src')
from agents_chat.v2.core.agent import Agent
from agents_chat.v2.scanner import Scanner
from agents_chat.v2.scheduler import Scheduler
from agents_chat.v2.infra.cli import OpenCodeCLI
from agents_chat.v2.infra.files import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')
MAX_ROUNDS = $MAX_ROUNDS_VAL
TIMEOUT_SECS = $TIMEOUT_SECS_VAL

# init mailboxes
for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# CLI: timeout 保护 (单次 LLM call 超时 2 分钟)
cli = OpenCodeCLI(timeout_seconds=120)

# Agent
agent_seller = Agent(
    agent_id='seller-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'seller-fish',
    poll_interval=1.0, default_channel='fish-market',
)
agent_buyer = Agent(
    agent_id='buyer-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'buyer-fish',
    poll_interval=1.0, default_channel='fish-market',
)

scanner = Scanner(data_dir=DATA_DIR, scan_interval=1.0)
scheduler = Scheduler(data_dir=DATA_DIR, stale_ttl=60, grace_period=30, check_interval=15)

# round 计数器 (通过 mailbox 投递计数)
round_count = 0
start_time = time.time()

async def guard_task():
    '''Guard: 超时到达时, cancel 所有 run tasks.'''
    _timeout = TIMEOUT_SECS if TIMEOUT_SECS else 300
    while True:
        await asyncio.sleep(2)
        elapsed = time.time() - start_time
        if elapsed >= _timeout:
            print(f'[guard] timeout ({_timeout}s), cancelling all tasks...')
            for t in _run_tasks:
                if not t.done():
                    t.cancel()
            # 强制终止还在跑的 opencode subprocess
            import subprocess
            subprocess.run(['pkill', '-9', '-f', 'opencode'], capture_output=True)
            scanner.stop(); scheduler.stop()
            agent_seller.stop(); agent_buyer.stop()
            break

async def main():
    global round_count
    _run_tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(agent_seller.run()),
        asyncio.create_task(agent_buyer.run()),
        asyncio.create_task(scheduler.run()),
        asyncio.create_task(guard_task()),
    ]
    print(f'[run-all] started: 2 agents, MAX_ROUNDS=$MAX_ROUNDS, TIMEOUT=${TIMEOUT_SECS}s')
    try:
        await asyncio.gather(*_run_tasks)
    except KeyboardInterrupt:
        print('[run-all] KeyboardInterrupt')
        for t in _run_tasks:
            if not t.done():
                t.cancel()
        scanner.stop(); scheduler.stop()
        agent_seller.stop(); agent_buyer.stop()
    except asyncio.CancelledError:
        print('[run-all] CancelledError')
    finally:
        print('[run-all] stopped gracefully')

asyncio.run(main())
" > "$LOG_FILE" 2>&1 &
RUN_PID=$!
KILL_PIDS="$RUN_PID"
echo "  run-all pid=$RUN_PID, log=$LOG_FILE"

# 等待 agents 启动
sleep 8

# =============================================================================
# 轮次: god 发邮件, 每封间隔 OPENCODE_WAIT 秒
# =============================================================================
OPENCODE_WAIT=25   # 等待 opencode 处理 (minimax-m3-free 较慢)
POST="$VENV -m agents_chat.v2.main post fish-market --sender god --data-dir $DATA_DIR"

echo ""
echo "T=$((11)) Round 0: god @sell 开价"
$POST "@sell 你好, 卖鱼吗? 开个价" 2>&1 | tail -1
sleep $OPENCODE_WAIT

# 检查是否已 stop (guard timeout 触发了)
if ! kill -0 $RUN_PID 2>/dev/null; then
    echo "[guard] run-all already stopped, skipping remaining rounds"
    MAX_ROUNDS=0
fi

# Round 1-N: check MAX_ROUNDS before sending
# MAX_ROUNDS=2 → 发 Round 1 + Round 2 (共 3 封, Round 0 + 2 more)
if [ "${MAX_ROUNDS:-0}" -ge 1 ]; then
    echo ""
    echo "T=$((11+OPENCODE_WAIT+5)) Round 1: god @buy 还价 70"
    $POST "@buy seller 报 100 太贵, 70 卖不卖?" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if ! kill -0 $RUN_PID 2>/dev/null; then echo "[guard] stopped"; MAX_ROUNDS=0; fi
if [ "${MAX_ROUNDS:-0}" -ge 2 ]; then
    echo ""
    echo "T=$((11+2*(OPENCODE_WAIT+5))) Round 2: god @sell 接受 80"
    $POST "@sell buyer 出 70 太少, 80 行不行?" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if ! kill -0 $RUN_PID 2>/dev/null; then echo "[guard] stopped"; MAX_ROUNDS=0; fi
if [ "${MAX_ROUNDS:-0}" -ge 3 ]; then
    echo ""
    echo "T=$((11+3*(OPENCODE_WAIT+5))) Round 3: god @buy 接受 80"
    $POST "@buy seller 80 元, 成交!" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if ! kill -0 $RUN_PID 2>/dev/null; then echo "[guard] stopped"; MAX_ROUNDS=0; fi
if [ "${MAX_ROUNDS:-0}" -ge 4 ]; then
    echo ""
    echo "T=$((11+4*(OPENCODE_WAIT+5))) Round 4: god @sell 确认"
    $POST "@sell 成交! 明天来取鱼" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

if ! kill -0 $RUN_PID 2>/dev/null; then echo "[guard] stopped"; MAX_ROUNDS=0; fi
if [ "${MAX_ROUNDS:-0}" -ge 5 ]; then
    echo ""
    echo "T=$((11+5*(OPENCODE_WAIT+5))) Round 5: god @buy 确认"
    $POST "@buy 明天来取鱼, 80 元" 2>&1 | tail -1
    sleep $OPENCODE_WAIT
fi

# =============================================================================
# 主动 stop (达到 max rounds)
# =============================================================================
echo ""
echo "T=$((11+MAX_ROUNDS*(OPENCODE_WAIT+5))) ⏹ 达到 MAX_ROUNDS=$MAX_ROUNDS, 主动 stop"
# 优雅 stop: 等待进程自然退出 (最多 30s), 超时再 kill
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
echo "  验证"
echo "============================================"
echo ""
echo "=== fish-market 对话 (tail 20) ==="
$VENV -m agents_chat.v2.main tail fish-market --n 20 --data-dir "$DATA_DIR" 2>&1

echo ""
echo "=== run-all log 关键行 (ERROR/WARN/reply/STATUS) ==="
grep -E "ERROR|WARN|reply|STATUS|stopped|timeout|guard" "$LOG_FILE" | grep -v "^$" | head -30

echo ""
echo "=== sessions ==="
for f in "$DATA_DIR/sessions"/*.json; do
    [ -f "$f" ] || continue
    echo "--- $(basename $f) ---"
    python3 -c "import json,sys; d=json.load(open('$f')); [print(' ', s['session_id'], '|', s.get('topic',''), '| prog=', s.get('progress',0), '%') for s in d.get('sessions',[])]" 2>/dev/null
done

echo ""
echo "=== run-all 耗时 ==="
python3 -c "
import os, time
log_mtime = os.path.getmtime('$LOG_FILE')
elapsed = time.time() - $(date +%s) + (time.time() - log_mtime)
print(f'log 最后修改: {time.ctime(log_mtime)}')
" 2>/dev/null

echo ""
echo "============================================"
echo "  E2E 讨价还价 complete (MAX_ROUNDS=$MAX_ROUNDS, TIMEOUT=${TIMEOUT_SECS}s)"
echo "============================================"