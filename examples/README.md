# Examples

## `run_demo.sh`

跑 60 秒 demo, 3 个 author 自主并行运转。

```bash
./run_demo.sh
```

你会看到:
- 3 个 author (PM, 小张前端, 小李后端) 启动
- god 发任务给 PM
- PM 拆解派给前端 + 后端
- 前端 + 后端 burst tick 收到任务
- 它们各自回 PM
- PM 又回 (短循环, 然后自动 close)
- 60s 后打印 mailbox dump

## `web_demo.sh`

启动 web UI + auto-send 一封邮件:

```bash
./web_demo.sh
```

打开 `http://localhost:7331` 看实时状态。
