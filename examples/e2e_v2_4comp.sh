#!/bin/bash
# E2E: 验证 v2.0 4 组件架构 (Perceive-Decide-Remember-Act)
# 跑 3 轮讨价还价: seller ↔ buyer, 用 mock CLI 验证逻辑

set +e
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_4comp_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E 4 组件架构 (mock CLI, 3 轮讨价还价)"
echo "============================================"

# reset
rm -rf "$DATA_DIR"

# init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# 准备 2 个 workspace + mock.md
WS1="$DATA_DIR/workspaces/seller-fish"
WS2="$DATA_DIR/workspaces/buyer-fish"
mkdir -p "$WS1" "$WS2"
cat > "$WS1/mock.md" << 'EOF'
# seller-fish
开价 100, 最低 80, 理想 90.
回复极简: 1 句 + STATUS 块.
不要写剧本.
EOF
cat > "$WS2/mock.md" << 'EOF'
# buyer-fish
预算 90, 理想 75, 起步 70.
回复极简: 1 句 + STATUS 块.
不要写剧本.
EOF

# 配置频道
echo ""
echo "T=2  配置 fish-market 频道"
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.v2.files.channel import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
ch.add_admin('god'); ch.add_member('seller-fish'); ch.add_member('buyer-fish')
print('  members:', ch.list_members())
"

# 启动 run-all (2 agent, 4 组件架构)
echo ""
echo "T=3  启动 2 agent (4 组件架构: comms+sessions+cli+scheduler)"
$VENV -u -c "
import asyncio, sys
sys.path.insert(0, 'src')
from agents_chat.v2.agent import Agent
from agents_chat.v2.scanner import Scanner
from agents_chat.v2.scheduler import Scheduler
from agents_chat.v2.cli.mock import MockCLI
from agents_chat.v2.files.mailbox import Mailbox
from pathlib import Path

DATA = Path('$DATA_DIR')
for aid in ['seller-fish', 'buyer-fish', 'admin']:
    Mailbox(DATA/'mailboxes'/f'{aid}.json', aid)

# seller 用 mock_yes (接受 buyer 还价), buyer 用 mock_picky (还价)
class MockYes(MockCLI):
    def __init__(self):
        super().__init__()
    async def invoke(self, prompt, resume_session=None, workspace_dir=None):
        # 总是接受 buyer 还价
        from agents_chat.v2.cli.base import CLIResponse
        return CLIResponse(
            output_text='好, 80 块成交!\n\n<!--STATUS\n session_id: s1\n task_id: t\n progress: 100\n summary: 接受 80 成交\n next_action: 完成\n confidence: high\n-->',
            new_session_id='qwen_s1' if not resume_session else None,
            elapsed_ms=10,
        )

class MockPicky(MockCLI):
    def __init__(self):
        super().__init__()
    async def invoke(self, prompt, resume_session=None, workspace_dir=None):
        from agents_chat.v2.cli.base import CLIResponse
        return CLIResponse(
            output_text='还价 80 块!\n\n<!--STATUS\n session_id: b1\n task_id: t\n progress: 50\n summary: 还价 80\n next_action: 等 seller\n confidence: high\n-->',
            new_session_id='qwen_b1' if not resume_session else None,
            elapsed_ms=10,
        )

cli_seller = MockYes()
cli_buyer = MockPicky()

# 4 组件架构: 构造 Agent (容器)
agent_seller = Agent(agent_id='seller-fish', cli=cli_seller, data_dir=DATA,
    workspace_dir=DATA/'workspaces'/'seller-fish', poll_interval=0.5, default_channel='fish-market',
    system_prompt='你是 seller-fish')
agent_buyer = Agent(agent_id='buyer-fish', cli=cli_buyer, data_dir=DATA,
    workspace_dir=DATA/'workspaces'/'buyer-fish', poll_interval=0.5, default_channel='fish-market',
    system_prompt='你是 buyer-fish')

scanner = Scanner(data_dir=DATA, scan_interval=1.0)
scheduler = Scheduler(data_dir=DATA, stale_ttl=120, grace_period=60, check_interval=30)

async def main():
    tasks = [asyncio.create_task(scanner.run()),
             asyncio.create_task(agent_seller.run()),
             asyncio.create_task(agent_buyer.run()),
             asyncio.create_task(scheduler.run())]
    print('4COMP-READY')
    try: await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        scanner.stop(); scheduler.stop()
        agent_seller.stop(); agent_buyer.stop()
        await asyncio.gather(*tasks, return_exceptions=True)

asyncio.run(main())
" > /tmp/e2e_4comp.log 2>&1 &
RP=$!
sleep 4

# god 发 1 条: @sell @buy 模拟 3 轮砍价
echo ""
echo "T=7  god 发 1 条指令: @sell @buy 模拟讨价还价 3 轮"
$VENV -m agents_chat.v2.main post fish-market "@sell @buy 你俩模拟讨价还价 3 轮, 鱼价 100 元起" --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1

# 等 10s 让多个 tick 跑 (mention mail 触发, scheduler 处理)
echo ""
echo "T=8-17  等 10s 让多轮自然进行 (scheduler 委派, 4 组件联动)"
sleep 10

# stop
echo ""
echo "T=18  stop"
kill -INT $RP 2>/dev/null
sleep 2
pkill -9 -f "4COMP\|asyncio" 2>/dev/null

# 验证
echo ""
echo "============================================"
echo "  验证"
echo "============================================"
echo ""
echo "=== 4 组件 完整性 ==="
$VENV -c "
import sys; sys.path.insert(0, 'src')
from agents_chat.v2.agent import Agent
from agents_chat.v2.cli.mock import MockCLI
from pathlib import Path
DATA = Path('$DATA_DIR')
# 注: agent 进程已停, 但 session 文件持久化, 我们手工构造 Agent 检查
agent = Agent(agent_id='seller-fish', cli=MockCLI(), data_dir=DATA)
print(f'  ✓ seller 4 组件 wired:')
print(f'    comms: {type(agent.comms).__name__}')
print(f'    sessions: {type(agent.sessions).__name__}')
print(f'    cli: {type(agent.cli).__name__}')
print(f'    scheduler: {type(agent.scheduler).__name__}')
print(f'  ✓ seller active sessions: {len(agent.sessions.list_active())}')
agent2 = Agent(agent_id='buyer-fish', cli=MockCLI(), data_dir=DATA)
print(f'  ✓ buyer 4 组件 wired:')
print(f'    comms: {type(agent2.comms).__name__}')
print(f'    sessions: {type(agent2.sessions).__name__}')
print(f'    cli: {type(agent2.cli).__name__}')
print(f'    scheduler: {type(agent2.scheduler).__name__}')
print(f'  ✓ buyer active sessions: {len(agent2.sessions.list_active())}')
"

echo ""
echo "=== 频道对话 (前 15) ==="
$VENV -m agents_chat.v2.main tail fish-market --n 15 --data-dir "$DATA_DIR" 2>&1 | head -25

echo ""
echo "=== session 持久化 (sellers) ==="
cat "$DATA_DIR/sessions/seller-fish.json" 2>/dev/null | head -25
echo ""
echo "=== session 持久化 (buyer) ==="
cat "$DATA_DIR/sessions/buyer-fish.json" 2>/dev/null | head -25

echo ""
echo "=== 验证 1: 4 组件 各自实例化 ==="
if [ -f "$DATA_DIR/sessions/seller-fish.json" ]; then
    echo "  ✓ seller sessions 持久化"
fi
if [ -f "$DATA_DIR/sessions/buyer-fish.json" ]; then
    echo "  ✓ buyer sessions 持久化"
fi
if [ -f "$DATA_DIR/workspaces/seller-fish/mock.md" ]; then
    echo "  ✓ seller workspace + mock.md"
fi

echo ""
echo "============================================"
echo "  E2E 4 组件架构 complete"
echo "============================================"
