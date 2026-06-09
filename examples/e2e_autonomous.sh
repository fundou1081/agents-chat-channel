#!/bin/bash
# e2e_autonomous.sh — 全自主 agent, 无 god, agent 自己发起对话
#
# 架构:
#   god (admin) 发第一条消息启动 → seller + buyer 订阅 fish-market
#   → seller 轮询 → DecisionMaker 决定发言 → 写频道
#   → buyer 轮询 → DecisionMaker 决定回复 → ...
#   → 直到成交/超时
#
# 用法:
#   MAX_ROUNDS=4 TIMEOUT_SECS=180 bash examples/e2e_autonomous.sh

set +e
cd "$(dirname "$0")/.."

MAX_ROUNDS="${MAX_ROUNDS:-4}"       # agent 最多发言轮数
TIMEOUT_SECS="${TIMEOUT_SECS:-180}"  # 全局超时
DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_autonomous_e2e}"
VENV="${VENV:-.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/e2e_autonomous.log}"

echo "============================================"
echo "  E2E 自主 (无 god, agent 自己发起对话)"
echo "  MAX_ROUNDS=$MAX_ROUNDS  TIMEOUT_SECS=${TIMEOUT_SECS}s"
echo "============================================"

# =============================================================================
# 清理
# =============================================================================
KILL_PIDS=""
cleanup() {
    echo ""
    echo "[cleanup] stopping..."
    for pid in $KILL_PIDS; do
        kill -INT $pid 2>/dev/null || true
    done
    sleep 2
    pkill -9 -f "opencode" 2>/dev/null || true
    pkill -9 -f "agents_chat" 2>/dev/null || true
    echo "[cleanup] done"
}
trap cleanup EXIT INT TERM

# =============================================================================
# reset + init
# =============================================================================
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# =============================================================================
# WorkerFactory: 创建 workers (各自独立 workspace, 隔离配置)
# 每个 worker 有自己的 roles.md + skills/ + mcp/ + instructions/
# =============================================================================

echo "  ✓ workspace 配置已定义 (运行时自动创建 workspace 目录)"

# =============================================================================
# 配置频道 (admin=god 是管理员, 发第一条消息)
# enabled_workers: 只允许 seller-fish 和 buyer-fish 响应 (白名单)
# =============================================================================
echo ""
echo "T=2  配置频道成员 + admin 发第一条消息"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.v2.infra.files import Channel

ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
ch.add_admin('god')              # god 是频道管理员
ch.add_member('seller-fish')     # 卖方
ch.add_member('buyer-fish')      # 买方
ch.add_member('admin')           # 人类 admin

# 白名单: 只有 seller-fish 和 buyer-fish 能收到消息
# 其他 worker 即使在 members 里, Scanner 也不会投递
ch.set_enabled_workers(['seller-fish', 'buyer-fish'])
print('  admin:', ch.list_admins())
print('  members:', ch.list_members())
print('  enabled_workers:', ch.list_enabled_workers())

# god (admin) 发第一条消息: 发起讨论
ch.append(
    from_='god', type='mention', content='@seller-fish @buyer-fish 今天的鱼价行情怎么样? 开始报价吧',
    mentions=['seller-fish', 'buyer-fish'],
)
print('  god 发起: 今天的鱼价行情怎么样? 开始报价吧')
"

# =============================================================================
# 启动 agents (主动模式, 订阅 fish-market)
# =============================================================================
echo ""
echo "T=3  启动 run-all (主动模式, 订阅 fish-market)"

MAX_ROUNDS_VAL=$MAX_ROUNDS
TIMEOUT_SECS_VAL=$TIMEOUT_SECS

$VENV -c "
import asyncio, sys, time
sys.path.insert(0, 'src')
from agents_chat.v2.infra.worker_factory import WorkerFactory
from agents_chat.v2.infra.files import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')
MAX_ROUNDS = $MAX_ROUNDS_VAL
TIMEOUT_SECS = $TIMEOUT_SECS_VAL

# init mailboxes
for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# 角色模板 (Python 多行字符串, 避免 bash 变量传递问题)
SELLER_ROLE = '''你是 seller-fish (卖鱼小贩). 跟 buyer-fish 讨价还价卖鱼.
策略: 开价 100, 最低 80, 理想 90.
- 每次报价要留余地, 最后可以搭两条小黄鱼
- 成交后要礼貌确认, 图个吉利数字
回复格式: 简短报价 + STATUS 块:
<!--STATUS
 session_id: {session_id}
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 buyer/成交>
 confidence: high
-->'''

BUYER_ROLE = '''你是 buyer-fish (买鱼顾客). 跟 seller-fish 讨价还价买鱼.
策略: 预算 90, 起步 70, 90 就接受.
- 先看 seller 开价, 然后还价
- 可以接受搭赠品
- 成交后要确认质量
回复格式: 简短还价或接受 + STATUS 块:
<!--STATUS
 session_id: {session_id}
 task_id: bargain
 progress: <0-100>
 summary: <你说的>
 next_action: <等 seller/成交>
 confidence: high
-->'''

# WorkerFactory: 创建 workers (各自独立 workspace, 隔离配置)
# roles.md: 完整角色定义 (strategy/报价策略/回复格式)
# skills: 技能 (软链接到全局 skills 目录)
# mcp_servers: MCP 服务配置 (生成 stub JSON)
workers = WorkerFactory.create_all(
    {
        'seller-fish': {
            'cli_type': 'opencode',
            'mode': 'proactive',
            'subscriptions': ['fish-market'],
            'poll_interval': 3.0,
            'default_channel': 'fish-market',
            'role': '卖鱼小贩',
            'role_template': SELLER_ROLE,
            'skills': ['bargaining', 'fish-pricing'],
            'mcp_servers': ['fish-market-api'],
            'cli_config': {'timeout_seconds': 120},
        },
        'buyer-fish': {
            'cli_type': 'opencode',
            'mode': 'proactive',
            'subscriptions': ['fish-market'],
            'poll_interval': 3.0,
            'default_channel': 'fish-market',
            'role': '买鱼顾客',
            'role_template': BUYER_ROLE,
            'skills': ['bargaining', 'budget-management'],
            'mcp_servers': ['payment-service'],
            'cli_config': {'timeout_seconds': 120},
        },
    },
    data_dir=DATA_DIR,
)
agent_seller = workers['seller-fish']
agent_buyer = workers['buyer-fish']

start_time = time.time()
_run_tasks = []

async def guard_task():
    '''超时或发言轮数达到时 cancel 所有 tasks.'''
    while True:
        await asyncio.sleep(2)
        elapsed = time.time() - start_time
        if elapsed >= TIMEOUT_SECS:
            print(f'[guard] timeout ({TIMEOUT_SECS}s), cancelling...')
            for t in _run_tasks:
                if not t.done():
                    t.cancel()
            import subprocess
            subprocess.run(['pkill', '-9', '-f', 'opencode'], capture_output=True)
            agent_seller.stop(); agent_buyer.stop()
            break

async def main():
    global _run_tasks
    _run_tasks = [
        asyncio.create_task(agent_seller.run()),
        asyncio.create_task(agent_buyer.run()),
        asyncio.create_task(guard_task()),
    ]
    print(f'[run-all] started: proactive mode, subscriptions=[fish-market], TIMEOUT={TIMEOUT_SECS}s')
    try:
        await asyncio.gather(*_run_tasks)
    except KeyboardInterrupt:
        print('[run-all] KeyboardInterrupt')
        for t in _run_tasks:
            if not t.done():
                t.cancel()
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

# =============================================================================
# 等待
# =============================================================================
echo ""
echo "T=5  等待 agent 自主发言 (TIMEOUT=${TIMEOUT_SECS}s)..."
sleep $((TIMEOUT_SECS + 5))

# =============================================================================
# stop
# =============================================================================
echo ""
echo "T=$((TIMEOUT_SECS + 10)) stop run-all"
kill -INT $RUN_PID 2>/dev/null || true
echo "  waiting for exit..."
_deadline=$(($(date +%s) + 30))
while kill -0 $RUN_PID 2>/dev/null; do
    if [ $(date +%s) -gt $_deadline ]; then
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
echo "=== fish-market 对话 (全部) ==="
$VENV -m agents_chat.v2.main tail fish-market --n 50 --data-dir "$DATA_DIR" 2>&1

echo ""
echo "=== run-all log 关键行 ==="
grep -E "run_all|guard|ERROR|WARN|reply|STATUS|stopped|proactive" "$LOG_FILE" | grep -v "^$" | head -20

echo ""
echo "=== 发言统计 ==="
$VENV -c "
import json, sys
sys.path.insert(0, 'src')
from agents_chat.v2.infra.files import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
msgs = ch.tail(100)
counts = {}
for m in msgs:
    f = m.get('from', '?')
    t = m.get('type', '?')
    key = f'{f} ({t})'
    counts[key] = counts.get(key, 0) + 1
for k, v in sorted(counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v} 条')
" 2>&1

echo ""
echo "============================================"
echo "  E2E 自主 complete (MAX_ROUNDS=$MAX_ROUNDS, TIMEOUT=${TIMEOUT_SECS}s)"
echo "============================================"