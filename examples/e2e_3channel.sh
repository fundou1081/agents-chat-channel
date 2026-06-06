#!/bin/bash
# E2E 3-channel test
set +e  # 关闭 errexit, 允许中间失败

cd /Users/fundou/.openclaw/workspace/agents-chat-channel
source .venv/bin/activate

echo "============================================"
echo "  E2E 3-channel test"
echo "============================================"

# 清理
rm -rf data/ /tmp/agents-chat-workdirs/
mkdir -p /tmp/agents-chat-workdirs/zhang /tmp/agents-chat-workdirs/li /tmp/agents-chat-workdirs/pm

# 杀旧 web (忽略没找到的错误)
pkill -9 -f "agents_chat.main web" 2>/dev/null
sleep 1

# 启动 web
echo ""
echo "T=0  启动 web (auto 模式)..."
python -u -m agents_chat.main web --llm auto --port 7331 > /tmp/e2e_web.log 2>&1 &
WEB_PID=$!
echo "  web pid=$WEB_PID"
sleep 6  # 等 author 起来

# 1. god 发 broadcast post
echo ""
echo "T=5  god 发 broadcast post (公告: 团队周会)"
curl -s -X POST http://localhost:7331/api/posts/post \
  -H "Content-Type: application/json" \
  -d '{"kind":"broadcast","title":"团队周会","body":"今天 3pm, 所有人","posted_by":"god"}' > /tmp/r1.json
python3 -c "import json; d=json.load(open('/tmp/r1.json')); print(f'  posted: id={d[\"id\"]} kind={d[\"kind\"]} title={d[\"title\"]}')"

# 2. god 发 task post
echo ""
echo "T=8  god 发 task post (frontend role)"
curl -s -X POST http://localhost:7331/api/posts/post \
  -H "Content-Type: application/json" \
  -d '{"kind":"task","title":"新首页 UI","body":"写一个 hello.py, 包含 hello() 函数返回 Hello","required_role":"frontend","tags":["前端","ui"],"posted_by":"god"}' > /tmp/r2.json
TASK_ID=$(python3 -c "import json; print(json.load(open('/tmp/r2.json'))['id'])")
echo "  posted task: id=$TASK_ID"

# 3. god 发 DM 给 PM
echo ""
echo "T=10 god 发 DM 给 PM (派活 + 任务详细)"
curl -s -X POST http://localhost:7331/api/send \
  -H "Content-Type: application/json" \
  -d '{"sender":"god","to":"pm","subject":"[任务] 写个 hello.py","body":"请安排团队完成, 用前端工程师","priority":9,"requires_ack":true}' > /tmp/r3.json
python3 -c "import json; d=json.load(open('/tmp/r3.json')); print(f'  sent DM: mail_id={d[\"mail_id\"]} thread_id={d[\"thread_id\"]}')"

# 4. god 建频道
echo ""
echo "T=12 god 建频道 #frontend + 邀请 zhang/li"
curl -s -X POST http://localhost:7331/api/channels/create \
  -H "Content-Type: application/json" \
  -d '{"name":"#frontend","description":"前端技术讨论","pinned_topic":"React 19 升级讨论","created_by":"god"}' > /tmp/r4.json
CH_ID=$(python3 -c "import json; print(json.load(open('/tmp/r4.json'))['id'])")
echo "  channel created: id=$CH_ID"
curl -s -X POST "http://localhost:7331/api/channels/$CH_ID/join" \
  -H "Content-Type: application/json" \
  -d '{"author_id":"zhang-frontend"}' > /dev/null
curl -s -X POST "http://localhost:7331/api/channels/$CH_ID/join" \
  -H "Content-Type: application/json" \
  -d '{"author_id":"li-backend"}' > /dev/null
echo "  zhang-frontend + li-backend joined"

# 5. god 发频道消息
echo ""
echo "T=14 god 发频道消息"
curl -s -X POST "http://localhost:7331/api/channels/$CH_ID/post" \
  -H "Content-Type: application/json" \
  -d '{"sender":"god","body":"@zhang-frontend 看一下 React 19 升级方案, 这周反馈"}' > /tmp/r5.json
python3 -c "import json; d=json.load(open('/tmp/r5.json')); print(f'  channel msg: id={d[\"id\"]} mentions={d[\"mentions\"]}')"

# 6. 等
echo ""
echo "============================================"
echo "  T=15-150: 让 author tick (PM qwen, zhang opencode)"
echo "============================================"
for t in 30 60 90 120 150 180 210 240 270 300 330 360 390 420; do
    sleep 15
    echo ""
    echo "--- T=$t ---"
    curl -s http://localhost:7331/api/authors > /tmp/authors.json
    python3 -c "
import json
for a in json.load(open('/tmp/authors.json')):
    sess = len(a['active_sessions'])
    print(f'  {a[\"persona\"][\"emoji\"]} {a[\"persona\"][\"display_name\"]:6s} status={a[\"status\"]:10s} ticks={a[\"total_ticks\"]:3d} actions={a[\"total_actions\"]} sessions={sess}')
"
done

echo ""
echo "============================================"
echo "  最终状态"
echo "============================================"

# 杀 web
kill $WEB_PID 2>/dev/null
sleep 1
pkill -9 -f "agents_chat.main web" 2>/dev/null

# 检查 mailbox
echo ""
echo "=== Mailbox (DM) ==="
python3 -c "
import sqlite3
conn = sqlite3.connect('data/mailbox.db')
c = conn.cursor()
c.execute('SELECT sender, recipients, subject, created_at FROM mails ORDER BY created_at')
for r in c.fetchall():
    print(f'  [{r[3][11:19]}] {r[0]:>15s} → {r[1][:30]:<30s} | {r[2][:50]}')
"

# 检查 posts
echo ""
echo "=== Posts ==="
python3 -c "
import sqlite3
conn = sqlite3.connect('data/posts.db')
c = conn.cursor()
c.execute('SELECT kind, title, status, claimed_by, posted_by FROM posts ORDER BY posted_at')
for r in c.fetchall():
    cb = r[3] or '-'
    print(f'  [{r[0]:15s}] {r[1]:25s} status={r[2]:10s} claimed_by={cb:15s} posted_by={r[4]}')
"

# 检查 channels
echo ""
echo "=== Channels ==="
python3 -c "
import sqlite3
conn = sqlite3.connect('data/channels.db')
c = conn.cursor()
c.execute('SELECT id, name, created_by FROM channels')
for r in c.fetchall():
    c2 = conn.cursor()
    c2.execute('SELECT COUNT(*) FROM channel_members WHERE channel_id = ?', (r[0],))
    members = c2.fetchone()[0]
    c3 = conn.cursor()
    c3.execute('SELECT COUNT(*) FROM channel_messages WHERE channel_id = ?', (r[0],))
    msgs = c3.fetchone()[0]
    print(f'  {r[1]} (id={r[0][:8]}..)  by={r[2]:6s}  {members} members, {msgs} msgs')
"
echo "  --- messages ---"
python3 -c "
import sqlite3
conn = sqlite3.connect('data/channels.db')
c = conn.cursor()
c.execute('SELECT sender, body, posted_at FROM channel_messages ORDER BY posted_at')
for r in c.fetchall():
    print(f'  [{r[2][11:19]}] {r[0]:>15s}: {r[1][:80]}')
"

# 检查 monitor
echo ""
echo "=== Monitor events (last 15) ==="
tail -15 data/logs/monitor.jsonl 2>/dev/null | python3 -c "
import json, sys
for line in sys.stdin:
    try:
        e = json.loads(line)
        ts = e['timestamp'][11:19]
        print(f'  {ts} | {e[\"kind\"]:20s} | {e[\"actor\"]:18s} | {e.get(\"summary\", \"\")[:60]}')
    except: pass
"

# 检查 workdir
echo ""
echo "=== Workdirs ==="
for d in pm zhang li; do
    echo "--- /tmp/agents-chat-workdirs/$d ---"
    ls -la /tmp/agents-chat-workdirs/$d/ 2>/dev/null | tail -5
    if [ -f /tmp/agents-chat-workdirs/$d/hello.py ]; then
        echo "  hello.py content:"
        cat /tmp/agents-chat-workdirs/$d/hello.py | head -10 | sed 's/^/    /'
    fi
done

# 跑 hello.py
echo ""
echo "=== zhang hello.py 实际跑 ==="
if [ -f /tmp/agents-chat-workdirs/zhang/hello.py ]; then
    cd /tmp/agents-chat-workdirs/zhang && python3 hello.py
    cd /Users/fundou/.openclaw/workspace/agents-chat-channel
fi

echo ""
echo "============================================"
echo "  E2E test complete"
echo "============================================"
