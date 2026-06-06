#!/bin/bash
# E2E 卖鱼/买鱼 (opencode CLI, 不同 workspace) - 真实模型
#
# 关键: opencode CLI 需要 ollama-cloud key (401) 或本地 ollama provider
# 这里改用 v2.0 QwenCLI (HTTP API 走本地 ollama) 作为 fallback,
# 实际架构跟 opencode 一样 (per-agent workspace + per-agent <cli>.md)

set +e
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_oc_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E 讨价还价 (QwenCLI via ollama, 2 不同 workspace)"
echo "============================================"

# reset
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

# init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# 准备 2 个不同的 workspace
WS1="$DATA_DIR/workspaces/seller-fish"
WS2="$DATA_DIR/workspaces/buyer-fish"
mkdir -p "$WS1" "$WS2"

# 写 seller-fish 角色
cat > "$WS1/qwen.md" << 'EOF'
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

如果对方接受你的价格, progress=100, summary=成交.
如果对方还价但太低, 拒绝并提你的理想价.
如果对方给个合理的中间价, 接受.

## 频道
- fish-market 频道里还有: god (admin), buyer-fish, admin
- @sell = 你 (seller-fish)
- @buy = buyer-fish
EOF

# 写 buyer-fish 角色
cat > "$WS2/qwen.md" << 'EOF'
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

如果对方降到 90 元以下, 接受并 progress=100.
如果还高于 90 元, 拒绝并提你理想价 75 元.
如果对方报 95, 还 80.

## 频道
- fish-market 频道里还有: god (admin), seller-fish, admin
- @sell = seller-fish
- @buy = 你 (buyer-fish)
EOF

echo "  ✓ 2 个 workspace + qwen.md 角色定义"

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

# 准备 run-all 命令 (用 QwenCLI, 走 ollama HTTP API)
echo ""
echo "T=3  启动 run-all (QwenCLI 调 ollama HTTP API, 2 agents, 不同 workspace)"

# 自定义 Python 启动: 每个 agent 用自己的 workspace
$VENV -c "
import asyncio, sys
sys.path.insert(0, 'src')
from agents_chat.v2.agent import Agent
from agents_chat.v2.scanner import Scanner
from agents_chat.v2.scheduler import Scheduler
from agents_chat.v2.cli.qwen import QwenCLI
from agents_chat.v2.files.mailbox import Mailbox
from pathlib import Path

DATA_DIR = Path('$DATA_DIR')

# 注册 agent mailboxes
for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA_DIR / 'mailboxes' / f'{aid}.json', aid)

# 构造 2 个 QwenCLI 实例 (共享 ollama daemon)
cli_seller = QwenCLI(model='minimax-m2.5:cloud', base_url='http://localhost:11434/v1', history_dir=DATA_DIR/'qwen_history')
cli_buyer = QwenCLI(model='minimax-m2.5:cloud', base_url='http://localhost:11434/v1', history_dir=DATA_DIR/'qwen_history')

# 2 个 agent 用不同 workspace + system_prompt
agent_seller = Agent(
    agent_id='seller-fish', cli=cli_seller, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'seller-fish',
    system_prompt='你是 seller-fish (卖鱼的小贩). 开价 100, 最低 80, 理想 90.',
    poll_interval=1.0, default_channel='fish-market',
)
agent_buyer = Agent(
    agent_id='buyer-fish', cli=cli_buyer, data_dir=DATA_DIR,
    workspace_dir=DATA_DIR/'workspaces'/'buyer-fish',
    system_prompt='你是 buyer-fish (买鱼的顾客). 预算最高 90, 理想 75, 起步 70.',
    poll_interval=1.0, default_channel='fish-market',
)

# Scanner + Scheduler
scanner = Scanner(data_dir=DATA_DIR, scan_interval=1.0)
scheduler = Scheduler(data_dir=DATA_DIR, stale_ttl=60, grace_period=30, check_interval=15)

async def main():
    tasks = [
        asyncio.create_task(scanner.run()),
        asyncio.create_task(agent_seller.run()),
        asyncio.create_task(agent_buyer.run()),
        asyncio.create_task(scheduler.run()),
    ]
    print('[run-all] started: 2 agents in 2 different workspaces')
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        scanner.stop(); scheduler.stop()
        agent_seller.stop(); agent_buyer.stop()
        await asyncio.gather(*tasks, return_exceptions=True)

asyncio.run(main())
" > /tmp/e2e_bargain_oc.log 2>&1 &
RUN_PID=$!
sleep 5

# 4 轮讨价还价 (god 主持, 用 @sell @buy 模糊匹配)
echo ""
echo "T=8  Round 0: god @sell 开价"
$VENV -m agents_chat.v2.main post fish-market "@sell 你好, 卖鱼吗? 开个价" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 6

echo ""
echo "T=14 Round 1: god @buy 还价 70"
$VENV -m agents_chat.v2.main post fish-market "@buy seller 报 100 太贵, 你 70 卖不卖?" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 6

echo ""
echo "T=20 Round 2: god @sell 接受 80"
$VENV -m agents_chat.v2.main post fish-market "@sell buyer 出 70 太少, 80 行不行?" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 6

echo ""
echo "T=26 Round 3: god @buy 接受 80"
$VENV -m agents_chat.v2.main post fish-market "@buy seller 80 元, 成交!" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 6

# stop
echo ""
echo "T=32 stop run-all"
kill -INT $RUN_PID 2>/dev/null
sleep 3
pkill -9 -f "QwenCLI\|run-all" 2>/dev/null

# 验证
echo ""
echo "============================================"
echo "  验证"
echo "============================================"
echo ""
echo "=== fish-market 完整对话 (tail 20) ==="
$VENV -m agents_chat.v2.main tail fish-market --n 20 --data-dir "$DATA_DIR" 2>&1 | head -25

echo ""
echo "=== run-all log 关键行 ==="
head -30 /tmp/e2e_bargain_oc.log

echo ""
echo "=== session 持久化 ==="
echo "--- seller-fish ---"
cat "$DATA_DIR/sessions/seller-fish.json" 2>/dev/null | head -20
echo ""
echo "--- buyer-fish ---"
cat "$DATA_DIR/sessions/buyer-fish.json" 2>/dev/null | head -20

echo ""
echo "=== 验证 1: 频道 metadata ==="
cat "$DATA_DIR/channels/fish-market.jsonl.meta.json" 2>/dev/null

echo ""
echo "=== 验证 2: workspace qwen.md ==="
ls "$DATA_DIR/workspaces/seller-fish/" 2>/dev/null
ls "$DATA_DIR/workspaces/buyer-fish/" 2>/dev/null

echo ""
echo "============================================"
echo "  E2E 讨价还价 (opencode/QwenCLI) complete"
echo "============================================"
