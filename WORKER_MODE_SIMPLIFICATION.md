# Worker 运行模式简化

## 🎯 决策背景

### 问题分析

之前的设计中，Worker 有两种"运行模式"：
- **Passive 模式**：等待 @mention，处理邮件
- **Proactive 模式**：订阅频道，主动轮询发言

但实际上，**两种模式都需要工作**：
1. Passive 模式的 Worker 也需要轮询邮箱检查新邮件
2. Proactive 模式的 Worker 也需要处理 @mention（也会收到邮件）

所以"模式"这个概念本身就没有意义了。

---

## ✅ 新的设计

### 统一的工作方式

**所有 Worker 都同时做两件事**：
1. **监听邮箱**（被动）：处理 @mention 和邮件
2. **轮询订阅频道**（主动）：如果有订阅，DecisionMaker 决定是否发言

```python
async def run(self):
    """主循环. 同时处理被动和主动模式."""
    
    # 如果有订阅，启动主动轮询（后台任务）
    if self.subscriptions:
        for ch in self.subscriptions:
            self.event_handler.add_subscription(ch)
        asyncio.create_task(self.event_handler.run_proactive())
    
    # 主循环：监听邮箱事件（被动模式）
    await self.event_handler.run()
```

---

## 📁 修改的文件

### 1. src/agents_chat/v2/agent.py

**位置**: 第 187-201 行  
**修改**: 移除 mode 判断，统一运行两种方式

```python
# 之前
if self.mode == "proactive" and self.subscriptions:
    await self.event_handler.run_proactive()
else:
    await self.event_handler.run()

# 现在
if self.subscriptions:
    asyncio.create_task(self.event_handler.run_proactive())
await self.event_handler.run()
```

---

### 2. src/agents_chat/v2/server.py

**位置**: 第 463-530 行  
**修改**: 
- 移除 `mode` 参数
- 保留 `subscriptions` 参数
- 更新 config.json 时移除 mode 字段

```python
# 之前
mode = body.get("mode", "passive")
if mode == "proactive":
    subscriptions = body.get("subscriptions", [])

# 现在
subscriptions = body.get("subscriptions", [])  # 直接获取
```

---

### 3. src/agents_chat/v2/worker_factory.py

**修改**:
- `_write_config()`: 移除 mode 参数
- `init()`: 移除 mode 参数
- `_init_workspace()`: 移除 mode 参数

```python
# 之前
def _write_config(self, cli_name, role, skills, mcp_servers, mode="passive", subscriptions=None):
    cfg = {
        "cli": cli_name,
        "mode": mode,  # ← 移除
        ...
    }

# 现在
def _write_config(self, cli_name, role, skills, mcp_servers, subscriptions=None):
    cfg = {
        "cli": cli_name,
        # 没有 mode 字段
        ...
    }
```

---

### 4. data_v2/workspaces/*/config.json

**修改**: 移除 mode 字段

```json
// 之前
{
  "agent_id": "seller-fish",
  "cli": "opencode",
  "mode": "passive",  // ← 移除
  "subscriptions": ["general"]
}

// 现在
{
  "agent_id": "seller-fish",
  "cli": "opencode",
  "subscriptions": ["general"]
}
```

---

## 💡 使用方式

### 创建 Worker

#### 之前（需要选择模式）
```javascript
payload = {
  mode: 'proactive',  // ← 需要选择
  cli_type: 'opencode',
  subscriptions: ['general']
}
```

#### 现在（只需指定订阅）
```javascript
payload = {
  cli_type: 'opencode',
  subscriptions: ['general']  // ← 有订阅就会主动轮询
}
```

**逻辑**：
- 有 `subscriptions` → 会主动轮询这些频道
- 没有 `subscriptions` → 只处理 @mention

---

### 查看 Worker 状态

Workers 视图中不再显示"运行模式"，只显示：
- CLI 类型
- 订阅频道（如果有）
- PDR 状态

---

## 🔧 技术细节

### 1. 并发执行

Worker 现在同时运行两个异步任务：

```python
# 任务 1: 后台轮询订阅频道（主动）
asyncio.create_task(self.event_handler.run_proactive())

# 任务 2: 主循环监听邮箱（被动）
await self.event_handler.run()
```

**好处**：
- ✅ 不会阻塞
- ✅ 两种方式并行工作
- ✅ 资源利用更高效

---

### 2. 订阅管理

```python
# 有订阅 → 主动轮询
if self.subscriptions:
    for ch in self.subscriptions:
        self.event_handler.add_subscription(ch)
    asyncio.create_task(self.event_handler.run_proactive())

# 没有订阅 → 只监听邮箱
# (run_proactive 不会启动)
```

---

### 3. 配置简化

**之前**：
```json
{
  "mode": "proactive",
  "subscriptions": ["general"]
}
```

**现在**：
```json
{
  "subscriptions": ["general"]
}
```

**逻辑**：
- 有 `subscriptions` → 自动启用主动轮询
- 没有 `subscriptions` → 只被动响应

---

## ✨ 优势对比

| 特性 | 之前 | 现在 |
|------|------|------|
| **概念复杂度** | ❌ 需要理解两种模式 | ✅ 简单直观 |
| **配置字段** | ❌ mode + subscriptions | ✅ 只有 subscriptions |
| **工作方式** | ❌ 二选一 | ✅ 两者兼顾 |
| **灵活性** | ❌ 模式固定 | ✅ 动态订阅 |
| **代码量** | ❌ 更多分支 | ✅ 更简洁 |

---

## 📊 用户体验提升

### 之前的问题
```
用户: "我要创建一个 Worker"
1. 选择 CLI 类型
2. 选择运行模式 ⬅️ 困惑
   - Passive: 只等 @mention？
   - Proactive: 主动发言？
3. 如果选 Proactive，填订阅
4. 担心选错模式
```

### 现在的体验
```
用户: "我要创建一个 Worker"
1. 选择 CLI 类型
2. （可选）填写订阅频道
   - 有订阅 → 会主动关注这些频道
   - 没订阅 → 只响应 @mention
3. 完成！✅
```

**改进**：
- 减少 1 个选择步骤
- 消除概念困惑
- 更符合直觉

---

## 🚀 未来扩展

### 1. 动态订阅管理

WebUI 中添加订阅管理功能：
```
Workers → 选择 Worker → [管理订阅]
→ 添加/移除订阅频道
→ 立即生效
```

### 2. 订阅推荐

根据 Worker 的角色自动推荐订阅：
```
客服类 Worker → 推荐订阅 support 频道
监控类 Worker → 推荐订阅 alerts 频道
```

### 3. 订阅统计

显示每个频道的活跃度：
```
订阅频道:
- general (10 条/小时)
- alerts (2 条/天)
```

---

## 🎯 总结

### 核心价值
1. **简化概念**: 移除无意义的"模式"区分
2. **统一行为**: 所有 Worker 都同时做两件事
3. **配置精简**: 只需关注订阅列表
4. **更灵活**: 动态订阅，无需重启

### 技术亮点
- 并发执行两种任务
- 配置驱动行为
- 向后兼容性好
- 代码更简洁

---

现在 Worker 的工作方式更简单了！🎊

**核心原则**：
- 所有 Worker 都会监听邮箱（处理 @mention）
- 如果有订阅，还会主动轮询频道
- 不需要选择"模式"，只需要指定"订阅"

刷新浏览器访问 http://127.0.0.1:8765，切换到"Workers"视图，就能看到简化的配置了！
