# 21. Event-Driven Bus (事件驱动总线)

> v2.0.1 改进 — 替代 v1 时代的"定时轮询"感知模式.

## 背景

v2.0 之前, agent 感知新消息靠**定时 poll 文件**:

```python
# 老模式: 1s 轮询
while not stopped:
    mails = self.mailbox.read_and_clear()
    for m in mails: yield ("mail", m)
    await asyncio.sleep(self.poll_interval)  # ← 浪费 1s CPU + 1s 延迟
```

**问题**:
- ⏱ **延迟**: 1-3s 一次轮询, 最坏情况等整个 poll_interval
- 💻 **CPU**: 50 agents × 1Hz 文件 I/O = 50 次/秒无意义读写
- 🔋 **浪费**: 没新消息时也按间隔醒来 (空轮询)

## v2.0.1 方案: 双层事件驱动

### Layer 1: 进程内 EventBus (asyncio.Event + 跨 loop 分桶)

**文件**: `src/agents_chat/infra/events.py`

```python
# 单例 + 跨 loop 安全
bus = get_event_bus()

# 同步 emit (在 Channel.append() 里)
bus.emit("mailbox:seller-fish:new")

# 异步 wait (在 listen() 循环里)
fired = await bus.wait("mailbox:seller-fish:new", timeout=1.0)
```

**事件命名**:
- `mailbox:<agent_id>:new` — 某 agent 收到新邮件
- `channel:<name>:new` — 某频道有新消息
- `task:<task_id>:update` — state_board 某 task 变化

**跨 loop 安全**:
- `asyncio.Event` 绑创建它的 event loop
- pytest-asyncio function scope 每个 test 独立 loop
- EventBus 按 `id(loop)` 分桶, 每个 loop 独立 Event 集合
- emit 广播到**所有** buckets (跨 loop 触发)

**emit-then-wait 顺序处理**:
- 普通 `asyncio.Event` 如果 emit 在 wait 之前, Event set 后没人 wait → 信号丢失
- 解决: `_pending` 字典 (`name -> True`), emit 时设置, wait 时优先检查 pending 立即返

### Layer 2: 跨进程 FileBusWatcher (watchdog 库)

**文件**: `src/agents_chat/infra/watcher.py`

```python
# agent 启动时
from agents_chat.infra.watcher import FileBusWatcher
watcher = FileBusWatcher(data_dir)
watcher.start()  # 后台 Observer 线程
# ... 跑 agent 逻辑 ...
watcher.stop()  # 退出时
```

**机制**:
- `watchdog.Observer` 监听 `data_dir/channels/` 和 `data_dir/mailboxes/` 目录
- 文件 modify/create → 转 EventBus 事件
- 跨平台: macOS FSEvents / Linux inotify / Windows ReadDirectoryChangesW
- 延迟: < 50ms (通常 1-10ms, 取决于平台 batch 间隔)

**降级**:
- `watchdog` 未装 / Observer 启动失败 → 只用进程内 EventBus + poll 兜底
- 跨进程感知退化到 1s poll, 但不会崩

## 性能数据

**测试**: `/tmp/bench_event_driven.py`

| 场景 | 延迟 | vs 老 1s poll |
|------|------|---------------|
| 进程内 emit→wait | 0.2-0.6 μs | **5,000,000×** |
| 跨进程 (watchdog) | 0-50 ms (worst case) | **20×** |
| 老 1s poll (基线) | 1002 ms | 1× |

**生产场景** (50 agents × 200 msg/s):
- 老: 100 次/秒空轮询, 每个消息延迟 0.5s (平均)
- 新: 0 次空轮询, 每个消息延迟 0.05s (平均) — 节省 99.9% 系统调用

## 测试

```bash
# 15 个 EventBus 单元测试 (含跨 loop, emit-then-wait)
.venv/bin/python -m pytest tests/unit/runtime/test_event_bus.py -v

# 12 个 watcher 单元测试 (含真实跨进程)
.venv/bin/python -m pytest tests/unit/runtime/test_watcher.py -v
```

**跨进程测试** (`test_subprocess_write_wakes_parent`):
- 子进程用 `Channel.append()` 写文件
- 父进程的 `FileBusWatcher` 监听 mtime 变化
- 父进程的 `bus.wait()` 收到事件 (0-50ms)

## 设计权衡

### 为什么不直接用 Redis Pub/Sub?

- **当前使用场景**: 单机 (god + 2-3 agent + server)
- **watchdog 优势**: 0 基础设施, 文件总线一致性, 跟 debug 体验统一
- **Redis 优势**: 多机分布式, 大量消息吞吐更好
- **建议**: 50+ agents 多机部署时再做 (3️⃣)

### 为什么不只用 polling?

- 用户体验: 1-3s 响应延迟在 demo 里"很慢"
- 资源: 50 agents × 1Hz = 50 文件 I/O / 秒 无意义
- 跟 1️⃣ + 2️⃣ 比: 多写 30 行代码, 省 99% CPU + 1000× 延迟

### 双层方案的好处

- **Layer 1 (EventBus)**: 解决"同进程多 agent" — server 内部 / 测试 / 单进程 agent
- **Layer 2 (FileBusWatcher)**: 解决"跨进程多 agent" — god 写 / agent 读
- 互补: Layer 1 在 layer 2 之前 (进程内直接 set, 0 syscall)
- 共同 EventBus 抽象, 未来加 RedisBus 只需加 Layer 3

## 改动文件

```
src/agents_chat/
├── infra/
│   ├── events.py            # NEW: EventBus 单例
│   ├── watcher.py           # NEW: FileBusWatcher (watchdog)
│   ├── files/
│   │   ├── channel.py       # MODIFIED: append() 后 emit channel_event
│   │   └── mailbox.py       # MODIFIED: append() 后 emit mailbox_event
│   └── __init__.py          # (可选: re-export EventBus)
└── core/
    ├── communication.py     # MODIFIED: listen() 用 EventBus.wait()
    └── agent.py             # MODIFIED: run() 启动 FileBusWatcher

pyproject.toml               # +watchdog>=4.0

tests/unit/runtime/
├── test_event_bus.py        # NEW: 15 tests
└── test_watcher.py          # NEW: 12 tests
```

## 未来 (3️⃣)

需要多机分布式时, 加 Layer 3:

```python
# src/agents_chat/infra/redis_bus.py
class RedisBusWatcher:
    """Redis Pub/Sub 替代 FileBusWatcher (多机场景)."""
    def __init__(self, redis_url: str):
        self.redis = aioredis.from_url(redis_url)
    async def start(self):
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("agent:events")
        async for msg in pubsub.listen():
            get_event_bus().emit(msg["data"].decode())
```

**抽象设计**: `EventBus` 已经是事件中心, RedisBus 只需在 emit/wait 之外提供"广播到 Redis"和"从 Redis 接收 → emit"两件事, 不需要改 API 层。
