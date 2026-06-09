# archive/

历史档案目录。仅保留**退役源码**, 不保留规划文档、临时数据、一次性工作日志。

## 当前内容

| 文件 | 类型 | 说明 |
|------|------|------|
| `bulletin_db.py.archived-2026-06-06` | 退役源码 | v1.x 的 bulletin SQLite 存储层. v2.0 用文件总线 + StateBoard 替代. 保留用于对照 evolution. |

## 清理历史 (2026-06-09)

合并到 v2.0 目录结构时已清理:

- ❌ `webui_plans/` — 23 个接手人留下的 webui 规划/设计文档 (v2.0 WebUI 已实装在 `src/agents_chat/v2/webui/`)
- ❌ `data_v1/` — 4 个 v1.x SQLite DB (bulletins / mailbox / sessions / rate_limits)
- ❌ `memory/2026-06-06.md` — 3 频道架构讨论的一次性工作日志

所有上述内容都已 commit 到 git history. 如需查阅, 用 `git log --all -- archive/webui_plans/` 等命令检索.

## 清理原则

新提交**不**应放 archive/ . 如需临时记录, 用 git stash / git branch 处理.
