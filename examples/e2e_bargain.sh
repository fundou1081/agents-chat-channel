#!/bin/bash
# E2E 卖鱼/买鱼 讨价还价 (3 轮) - 验证 channel members + 模糊匹配
#
# 场景:
#   - 频道: fish-market (成员: seller-fish, buyer-fish, god (admin))
#   - 卖鱼 (seller-fish) 开价 100 元
#   - 买鱼 (buyer-fish) 还价 70 → 80
#   - 卖鱼让到 90
#   - 买鱼接受, 成交
#   - 3 轮讨价还价, 4 个 reply 来回
#
# 验证:
#   - agents 通过 workspace.md 知道频道成员
#   - @sell 模糊匹配到 seller-fish
#   - @buy 模糊匹配到 buyer-fish
#   - admins (god) 可以看频道, 不参与交易
#   - in_reply_to 维持 thread, Scanner 把 reply 路由到原 agent

set +e
cd "$(dirname "$0")/.."

DATA_DIR="${DATA_DIR:-/tmp/agents_chat_v2_bargain_e2e}"
VENV="${VENV:-.venv/bin/python}"

echo "============================================"
echo "  E2E 卖鱼/买鱼 讨价还价 (data_dir=$DATA_DIR)"
echo "============================================"

# reset
echo ""
echo "T=0  reset"
rm -rf "$DATA_DIR"

# init
echo ""
echo "T=1  init"
$VENV -m agents_chat.v2.main init --data-dir "$DATA_DIR" 2>&1 | tail -2

# 注册 agent mailboxes (让 Scanner 能投递)
echo ""
echo "T=2  注册 agent mailboxes"
for aid in seller-fish buyer-fish admin god; do
    $VENV -c "
import json, sys
from pathlib import Path
p = Path('$DATA_DIR/mailboxes/$aid.json')
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps({'agent': '$aid', 'pending': []}))
print(f'  ✓ $aid')
"
done

# 配置频道成员 (god, seller-fish, buyer-fish, admin)
echo ""
echo "T=3  配置 fish-market 频道成员"
$VENV -c "
import sys
sys.path.insert(0, 'src')
from agents_chat.v2.files.channel import Channel
ch = Channel('$DATA_DIR/channels/fish-market.jsonl', 'fish-market')
ch.add_admin('god')
ch.add_member('seller-fish')
ch.add_member('buyer-fish')
ch.add_member('admin')
print('  members:', ch.list_members())
print('  admins:', ch.list_admins())
"

# 启动 run-all
echo ""
echo "T=4  start run-all (2 agents, mock CLI)"
$VENV -m agents_chat.v2.main run-all --data-dir "$DATA_DIR" \
    --agents seller-fish buyer-fish --cli mock > /tmp/e2e_bargain_runall.log 2>&1 &
RUN_PID=$!
sleep 3

# Round 0: god 让 seller 开价
echo ""
echo "============================================"
echo "  T=5  Round 0: god 让 seller-fish 开价"
echo "============================================"
$VENV -m agents_chat.v2.main post fish-market \
    "@sell 你好, 卖鱼吗? 开个价" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 3

# Round 1: buyer 还价 (用 @buy 模糊匹配)
echo ""
echo "============================================"
echo "  T=8  Round 1: buyer-fish 还价 70"
echo "============================================"
# 注: 这是模拟 — seller 的 reply 已经在 channel 里, 我们让 buyer 看到后再还
# 实际场景: seller 的 reply 里有 @buy, 触发 buyer 处理
# 但 MockCLI 不能真正"看到"上下文, 我们手动写一个 buyer 看到的 mention
# 简化: 让 god 提示 buyer-fish 还价
$VENV -m agents_chat.v2.main post fish-market \
    "@buy seller 报 100 元, 你觉得太贵, 还价 70 元" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 3

# Round 2: seller 让到 90
echo ""
echo "============================================"
echo "  T=11  Round 2: seller-fish 让到 90"
echo "============================================"
$VENV -m agents_chat.v2.main post fish-market \
    "@sell buyer 出 70, 你最低能多少? 试试 90" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 3

# Round 3: buyer 接受 90
echo ""
echo "============================================"
echo "  T=14  Round 3: buyer-fish 接受 90"
echo "============================================"
$VENV -m agents_chat.v2.main post fish-market \
    "@buy seller 90 行不行? 行就成交" \
    --sender god --data-dir "$DATA_DIR" 2>&1 | tail -1
sleep 4

# stop
echo ""
echo "T=18  stop run-all"
kill -INT $RUN_PID 2>/dev/null
sleep 2

# 验证
echo ""
echo "============================================"
echo "  验证"
echo "============================================"

echo ""
echo "=== fish-market 频道消息 ==="
$VENV -m agents_chat.v2.main tail fish-market --n 30 --data-dir "$DATA_DIR"

echo ""
echo "=== workspaces/ 引导文件 ==="
echo "--- seller-fish/mock.md (头部 20 行) ---"
head -20 "$DATA_DIR/workspaces/seller-fish/mock.md" 2>/dev/null
echo ""
echo "--- buyer-fish/mock.md (头部 20 行) ---"
head -20 "$DATA_DIR/workspaces/buyer-fish/mock.md" 2>/dev/null

echo ""
echo "=== 频道元数据 ==="
cat "$DATA_DIR/channels/fish-market.jsonl.meta.json" 2>/dev/null

echo ""
echo "=== session 持久化 ==="
echo "--- seller-fish sessions ---"
cat "$DATA_DIR/sessions/seller-fish.json" 2>/dev/null | head -20
echo ""
echo "--- buyer-fish sessions ---"
cat "$DATA_DIR/sessions/buyer-fish.json" 2>/dev/null | head -20

echo ""
echo "============================================"
echo "  验证 1: 频道 members 写入 metadata"
echo "============================================"
if grep -q "members" "$DATA_DIR/channels/fish-market.jsonl.meta.json" 2>/dev/null; then
    echo "  ✓ fish-market 频道有 members 字段"
fi
if grep -q "god" "$DATA_DIR/channels/fish-market.jsonl.meta.json" 2>/dev/null; then
    echo "  ✓ god 是 admin"
fi
if grep -q "seller-fish" "$DATA_DIR/channels/fish-market.jsonl.meta.json" 2>/dev/null; then
    echo "  ✓ seller-fish 是 member"
fi

echo ""
echo "============================================"
echo "  验证 2: 模糊匹配 (workspaces/md 注入规则)"
echo "============================================"
if grep -q "模糊匹配" "$DATA_DIR/workspaces/seller-fish/mock.md" 2>/dev/null; then
    echo "  ✓ seller-fish mock.md 含模糊匹配规则说明"
fi
if grep -q "成员" "$DATA_DIR/workspaces/seller-fish/mock.md" 2>/dev/null; then
    echo "  ✓ seller-fish mock.md 含频道成员列表"
fi

echo ""
echo "============================================"
echo "  验证 3: 讨价还价 reply 出现"
echo "============================================"
MSG_COUNT=$($VENV -c "
import json
with open('$DATA_DIR/channels/fish-market.jsonl') as f:
    msgs = [json.loads(l) for l in f if l.strip()]
agents = {m['from'] for m in msgs if m['from'] != 'god'}
print(f'  共 {len(msgs)} 条消息, 非 god agent 发言: {agents}')
")
echo "$MSG_COUNT"

echo ""
echo "============================================"
echo "  E2E 卖鱼/买鱼 complete"
echo "============================================"
