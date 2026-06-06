#!/bin/bash
# E2E 卖鱼/买鱼 (真 OpenCodeCLI + opencode/minimax-m3-free, 不同 workspace)
#
# 跟 e2e_bargain_opencode.sh (用 QwenCLI) 不同: 这个**直接用 OpenCodeCLI**
# 调 opencode CLI 的 minimax-m3-free 模型. 不同 agent 配不同 workspace_dir
# 让 opencode 读自己的 opencode.md.

set +e
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_real_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E 讨价还价 (OpenCodeCLI + minimax-m3-free, 2 workspace)"
echo "============================================"

# reset
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

# init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# 准备 2 个 workspace + opencode.md
WS1="$DATA_DIR/workspaces/seller-fish"
WS2="$DATA_DIR/workspaces/buyer-fish"
mkdir -p "$WS1" "$WS2"

cat > "$WS1/opencode.md" << 'EOF'
# seller-fish 角色

你是 seller-fish (卖鱼的小贩). 你正在跟 buyer-fish 讨价还价.

## 你的策略
- 开价: 100 元/条
- 最低: 80 元
- 理想: 90 元
- 不接受 80 以下

## 你的回复格式
简短回复 (一句话报价或拒绝) + STATUS 块:
<!--STATUS
 session_id: seller-sess
 task_id: bargain
 progress: <当前轮 0-100>
 summary: <你刚才说的>
 next_action: <等 buyer 回复 / 成交>
 confidence: high
-->
EOF

cat > "$WS2/opencode.md" << 'EOF'
# buyer-fish 角色

你是 buyer-fish (买鱼的顾客). 你正在跟 seller-fish 讨价还价.

## 你的策略
- 预算: 最高 90 元
- 理想: 75 元
- 起步: 70 元
- 90 元就接受, 90 以下才还价

## 你的回复格式
简短回复 (一句话还价或接受) + STATUS 块:
<!--STATUS
 session_id: buyer-sess
 task_id: bargain
 progress: <当前轮 0-100>
 summary: <你刚才说的>
 next_action: <等 seller 回复 / 成交>
 confidence: high
-->
EOF

echo "  ✓ 2 个 workspace + opencode.md 角色定义"

# 配置频道成员
echo ""
echo "T=2  配置 fish-market 频道成员"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.v2.files.channel import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
ch.add_admin('god')
ch.add_member('seller-fish')
ch.add_member('buyer-fish')
ch.add_member('admin')
print('  members:', ch.list_members())
print('  admins:', ch.list_admins())
"

# 启动 run-all (用 OpenCodeCLI + minimax-m3-free)
echo ""
echo "T=3  启动 run-all (OpenCodeCLI, 2 agents, 2 workspace)"
$VENV -c "
import asyncio, sys
sys.path.insert(0, 'src')
from agents_chat.v2.agent import Agent
from agents_chat.v2.scanner import Scanner
from agents_chat.v2.scheduler import Scheduler
from agents_chat.v2.cli.opencode import OpenCodeCLI
from agents_chat.v2.files.mailbox import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')

for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# OpenCodeCLI 默认用 opencode/minimax-m3-free
cli = OpenCodeCLI(timeout_seconds=120)

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

async def main():
    tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(agent_seller.run()),
        asyncio.create_task(agent_buyer.run()),
        asyncio.create_task(scheduler.run()),
    ]
    print('[run-all] started: 2 OpenCodeCLI agents in 2 workspaces')
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        scanner.stop(); scheduler.stop()
        agent_seller.stop(); agent_buyer.stop()
        await asyncio.gather(*tasks, return_exceptions=True)

asyncio.run(main())
" > /tmp/e2e_bargain_real.log 2>&1 &
RUN_PID=$!
sleep 10  # opencode 启动慢

# 4 轮讨价还价
echo ""
echo "T=13 Round 0: god @sell 开价"
$VENV -m agents_chat.v2.main post fish-market "@sell 你好, 卖鱼吗? 开个价" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 20  # opencode 调用慢

echo ""
echo "T=33 Round 1: god @buy 还价 70"
$VENV -m agents_chat.v2.main post fish-market "@buy seller 报 100 太贵, 70 卖不卖?" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 20

echo ""
echo "T=53 Round 2: god @sell 接受 80"
$VENV -m agents_chat.v2.main post fish-market "@sell buyer 出 70 太少, 80 行不行?" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 20

echo ""
echo "T=73 Round 3: god @buy 接受 80"
$VENV -m agents_chat.v2.main post fish-market "@buy seller 80 元, 成交!" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 20

# stop
echo ""
echo "T=93 stop run-all"
kill -INT $RUN_PID 2>/dev/null
sleep 3
pkill -9 -f "opencode" 2>/dev/null
pkill -9 -f "asyncio" 2>/dev/null

# 验证
echo ""
echo "============================================"
echo "  验证"
echo "============================================"
echo ""
echo "=== fish-market 完整对话 ==="
$VENV -m agents_chat.v2.main tail fish-market --n 15 --data-dir "$DATA_DIR" 2>&1 | head -25

echo ""
echo "=== run-all log 关键行 ==="
grep -E "agent_seller|agent_buyer|started|reply|STATUS" /tmp/e2e_bargain_real.log | head -20

echo ""
echo "=== session 持久化 ==="
echo "--- seller-fish ---"
cat "$DATA_DIR/sessions/seller-fish.json" 2>/dev/null | head -20
echo ""
echo "--- buyer-fish ---"
cat "$DATA_DIR/sessions/buyer-fish.json" 2>/dev/null | head -20

echo ""
echo "============================================"
echo "  E2E 讨价还价 (OpenCodeCLI 真实) complete"
echo "============================================"
