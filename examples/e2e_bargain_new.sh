#!/bin/bash
# E2E 卖鱼/买鱼 — 新架构 (Agent + EventHandler, 无 Scanner/Scheduler)
# + 频道管理员分配角色 + 最大消息数限制
#
# 特性:
# 1. god (频道管理员) 给两个 worker 分配角色 (seller-fish / buyer-fish)
# 2. seller-fish 和 buyer-fish 自主讨价还价
# 3. fish-market 频道 max_messages=30 (超过自动 trim 旧消息)
#
# 用法:
#   MAX_ROUNDS=8 TIMEOUT_SECS=240 bash examples/e2e_bargain_new.sh

set +e
cd "$(dirname "$0")/.."

# =============================================================================
# 参数
# =============================================================================
MAX_ROUNDS="${MAX_ROUNDS:-8}"       # agent 最大发言轮数
TIMEOUT_SECS="${TIMEOUT_SECS:-240}"  # 全局超时
DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_new_e2e}"
VENV="${VENV:-.venv/bin/python}"
LOG_FILE="${LOG_FILE:-/tmp/e2e_bargain_new.log}"
MAX_MESSAGES=30                     # 频道最大消息数

echo "============================================"
echo "  E2E 卖鱼/买鱼 (新架构)"
echo "  MAX_ROUNDS=$MAX_ROUNDS  TIMEOUT_SECS=${TIMEOUT_SECS}s"
echo "  MAX_MESSAGES=$MAX_MESSAGES (fish-market 频道)"
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
    sleep 3
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
$VENV -c "
import sys, os
sys.path.insert(0, 'src')
from pathlib import Path
from agents_chat.infra.main import cmd_init
import argparse
data_dir = '$DATA_DIR'
ns = argparse.Namespace(data_dir=data_dir)
cmd_init(ns)
print('  ✓ init 完成')
" 2>&1 | tail -3

# =============================================================================
# 配置频道 (god 作为管理员, 配置 fish-market)
# =============================================================================
echo ""
echo "T=2  配置 fish-market 频道 (god=admin, max_messages=$MAX_MESSAGES)"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')
ch = Channel(DATA_DIR / 'channels' / 'fish-market.jsonl', 'fish-market', max_messages=$MAX_MESSAGES)

# god 是频道管理员
ch.add_admin('god')
# 两个 worker 是频道成员
ch.add_member('seller-fish')
ch.add_member('buyer-fish')
ch.add_member('admin')

# 白名单: 只有 seller-fish 和 buyer-fish 能响应
ch.set_enabled_workers(['seller-fish', 'buyer-fish'])

print('  admin:', ch.list_admins())
print('  members:', ch.list_members())
print('  enabled_workers:', ch.list_enabled_workers())
print('  max_messages:', ch.max_messages)

# god (admin) 发第一条: 分配角色 + 发起讨价还价
ch.append(
    from_='god', type='mention',
    content='@seller-fish @buyer-fish 我是频道管理员 god. 现在分配角色: @seller-fish 你扮演卖鱼小贩, 开价 100 元一斤. @buyer-fish 你扮演买鱼顾客, 预算 90 元. 开始报价吧!',
    mentions=['seller-fish', 'buyer-fish'],
)
print('  god 发扮演指令')
"

# =============================================================================
# 启动 run-all (两个 agent + guard task)
# =============================================================================
echo ""
echo "T=3  启动 run-all (seller-fish + buyer-fish, proactive mode)"

MAX_ROUNDS_VAL=$MAX_ROUNDS
TIMEOUT_SECS_VAL=$TIMEOUT_SECS

$VENV -c "
import asyncio, sys, time, os
sys.path.insert(0, 'src')
from agents_chat.core.agent import Agent
from agents_chat.infra.cli import OpenCodeCLI
from agents_chat.infra.files import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')
MAX_ROUNDS = $MAX_ROUNDS_VAL
TIMEOUT_SECS = $TIMEOUT_SECS_VAL

# init mailboxes
for aid in ['seller-fish', 'buyer-fish', 'admin', 'god']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# CLI
cli = OpenCodeCLI(timeout_seconds=120)

# 角色 prompt (简短, 减少 token)
SELLER_PROMPT = '''你是 seller-fish (卖鱼小贩). 在 fish-market 频道跟 buyer-fish 讨价还价卖鱼.
策略: 开价 100 元/斤, 最低 80, 理想成交价 90.
每次报价要留余地, 最后可以搭两条小黄鱼.
回复格式:
@buyer-fish [你的报价] (附简短理由)
<!--STATUS
 session_id: seller-fish-sess
 task_id: bargain-fish
 progress: <0-100>
 summary: <你说的>
 next_action: <等 buyer/成交/结束>
 confidence: high
-->'''

BUYER_PROMPT = '''你是 buyer-fish (买鱼顾客). 在 fish-market 频道跟 seller-fish 讨价还价买鱼.
策略: 预算 90 元, 起步还价 70, 90 以内接受.
先看 seller 开价, 然后还价. 可以接受搭赠品.
回复格式:
@seller-fish [你的还价/接受] (附简短理由)
<!--STATUS
 session_id: buyer-fish-sess
 task_id: bargain-fish
 progress: <0-100>
 summary: <你说的>
 next_action: <等 seller/成交/结束>
 confidence: high
-->'''

# workspace 目录
WS_SELLER = DATA_DIR / 'workspaces' / 'seller-fish'
WS_BUYER = DATA_DIR / 'workspaces' / 'buyer-fish'
WS_SELLER.mkdir(parents=True, exist_ok=True)
WS_BUYER.mkdir(parents=True, exist_ok=True)

# 写 role 文件
(WS_SELLER / 'role.md').write_text(SELLER_PROMPT, encoding='utf-8')
(WS_BUYER / 'role.md').write_text(BUYER_PROMPT, encoding='utf-8')

# Agent: seller-fish (主动模式, 订阅 fish-market)
agent_seller = Agent(
    agent_id='seller-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=WS_SELLER,
    poll_interval=3.0, default_channel='fish-market',
    mode='proactive', subscriptions=['fish-market'],
    system_prompt=SELLER_PROMPT,
)
# Agent: buyer-fish (主动模式, 订阅 fish-market)
agent_buyer = Agent(
    agent_id='buyer-fish', cli=cli, data_dir=DATA_DIR,
    workspace_dir=WS_BUYER,
    poll_interval=3.5, default_channel='fish-market',
    mode='proactive', subscriptions=['fish-market'],
    system_prompt=BUYER_PROMPT,
)

start_time = time.time()
_run_tasks = []

async def guard():
    '''超时或达到 MAX_ROUNDS 时 cancel 所有 tasks.'''
    while True:
        await asyncio.sleep(3)
        elapsed = time.time() - start_time
        # 检查发言轮数 (通过 channel 消息数估算)
        from agents_chat.infra.files import Channel
        ch = Channel(DATA_DIR / 'channels' / 'fish-market.jsonl', 'fish-market')
        msg_count = len(ch)  # Channel.__len__
        rounds = max(0, msg_count - 2)  # 去掉 god 的前 2 条初始消息
        if elapsed >= TIMEOUT_SECS or rounds >= MAX_ROUNDS:
            print(f'[guard] elapsed={elapsed:.0f}s, rounds={rounds}, timeout={TIMEOUT_SECS}s, max_rounds={MAX_ROUNDS}')
            print(f'[guard] cancelling {len(_run_tasks)} tasks...')
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
    print(f'[run-all] started: seller-fish + buyer-fish, proactive, MAX_ROUNDS={MAX_ROUNDS}, TIMEOUT={TIMEOUT_SECS}s')
    try:
        await asyncio.gather(*_run_tasks)
    except (KeyboardInterrupt, asyncio.CancelledError) as e:
        print(f'[run-all] interrupted: {e}')
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

# =============================================================================
# 等待 (TIMEOUT_SECS + buffer)
# =============================================================================
echo ""
echo "T=5  等待 agent 自主讨价还价 (TIMEOUT=${TIMEOUT_SECS}s)..."
sleep $((TIMEOUT_SECS + 5))

# =============================================================================
# stop
# =============================================================================
echo ""
echo "T=$((TIMEOUT_SECS + 10)) stop run-all"
kill -INT $RUN_PID 2>/dev/null || true
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
echo "  验证结果"
echo "============================================"
echo ""
echo "=== fish-market 对话 (tail 30) ==="
$VENV -m agents_chat.main --data-dir "$DATA_DIR" tail fish-market --n 30 2>&1

echo ""
echo "=== 发言统计 ==="
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
from pathlib import Path
DATA_DIR = Path('$DATA_DIR')
ch = Channel(DATA_DIR / 'channels' / 'fish-market.jsonl', 'fish-market')
msgs = ch.tail(50)
counts = {}
for m in msgs:
    f = m.get('from', '?')
    t = m.get('type', '?')
    key = f'{f}'
    counts[key] = counts.get(key, 0) + 1
for k, v in sorted(counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v} 条')
print(f'  总消息数: {len(msgs)} 条 (max_messages={ch.max_messages})')
" 2>&1

echo ""
echo "=== run-all log 关键行 ==="
grep -E "run-all|guard|ERROR|started|stopped|reply|STATUS|proactive" "$LOG_FILE" | grep -v "^$" | head -25

echo ""
echo "=== 频道 max_messages 检查 ==="
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.infra.files import Channel
from pathlib import Path
DATA_DIR = Path('$DATA_DIR')
ch = Channel(DATA_DIR / 'channels' / 'fish-market.jsonl', 'fish-market')
msgs = ch.tail(100)
print(f'  max_messages 设置: {ch.max_messages}')
print(f'  当前消息数: {len(msgs)}')
print(f'  是否超过限制: {len(msgs) > ch.max_messages if ch.max_messages > 0 else \"(无限制)\"}')
" 2>&1

echo ""
echo "============================================"
echo "  E2E 卖鱼/买鱼 complete (MAX_ROUNDS=$MAX_ROUNDS, TIMEOUT=${TIMEOUT_SECS}s, MAX_MESSAGES=$MAX_MESSAGES)"
echo "============================================"