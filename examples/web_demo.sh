#!/bin/bash
# 启动 web UI, 然后 5s 后自动发一封邮件给 PM
cd "$(dirname "$0")/.."
source .venv/bin/activate
rm -rf data/
python -u -m agents_chat.main web --port 7331 &
WEB_PID=$!
echo "web pid: $WEB_PID"
echo "打开 http://localhost:7331"
echo "5s 后会自动发一封邮件给 PM"
sleep 5
curl -s -X POST http://localhost:7331/api/send \
  -H "Content-Type: application/json" \
  -d '{"sender":"god","to":"pm","subject":"[任务] 加搜索功能","body":"全局搜索 + 高亮匹配","priority":9,"requires_ack":true}'
echo ""
echo "刷新浏览器看效果"
echo "Ctrl-C 退出"
wait $WEB_PID
