# Examples

## 当前活跃脚本 (v2.0)

### `e2e_bargain_real.sh` — 讨价还价 e2e (被动模式)
真 OpenCodeCLI + opencode/deepseek-v4-flash-free, god 控制节奏:
```bash
MAX_ROUNDS=4 TIMEOUT_SECS=240 bash examples/e2e_bargain_real.sh
```
- god @sell 开价 → buyer 还价 → seller 让步 → 成交
- 2 agents, 独立 workspace, Scanner 投递 mail (被动模式)
- DecisionMaker 选 session + CLI 生成 reply

### `e2e_autonomous.sh` — 全自主 e2e (主动模式)
无 god, agent 自己订阅频道 + 主动发起对话:
```bash
MAX_ROUNDS=4 TIMEOUT_SECS=180 bash examples/e2e_autonomous.sh
```
- god 发第一条消息后退出
- seller + buyer 订阅 fish-market, 轮询频道
- DecisionMaker.decide_speak 决定要不要发言
- seller 5 条 + buyer 4 条自主对话, 88 元成交

## 归档 (archive/)

| 目录/文件 | 内容 |
|-----------|------|
| `archive/data_v1/` | 旧 v1.x SQLite DBs (bulletins/mailbox/sessions.db) |
| `archive/memory/` | agent 开发期内存文件 |
| `archive/bulletin_db.py.archived-2026-06-06` | 旧存档 |

## 旧脚本 (已删除)

| 脚本 | 原因 |
|------|------|
| `run_demo.sh` / `web_demo.sh` | v1.x demo (已废弃) |
| `e2e_3channel.sh` | v1.x (已废弃) |
| `e2e_bargain.sh` / `e2e_bargain_opencode.sh` | 旧版 v2.x (被 `e2e_bargain_real.sh` 取代) |
| `e2e_v2.sh` / `e2e_v2_4comp.sh` | 旧版 v2.x (被 `e2e_autonomous.sh` 取代) |
| `e2e_workspace.sh` | 旧测试 (已废弃) |