# 频道最大消息数编辑功能

## 🎯 功能概述

为已创建的频道添加了动态调整最大消息数（max_messages）的功能，无需删除重建频道。

---

## ✅ 实现内容

### 1. 后端 API

#### 新增端点
```python
PUT /api/channels/{name}/config
```

**请求体**:
```json
{
  "max_messages": 100
}
```

**响应**:
```json
{
  "ok": true,
  "channel": "general",
  "max_messages": 100
}
```

**功能**:
- ✅ 更新频道的 `max_messages` 配置
- ✅ 验证输入值（必须 >= 0）
- ✅ 保存到频道的 meta.json 文件
- ✅ 立即生效，无需重启

**错误处理**:
- 404: 频道不存在
- 400: max_messages < 0

---

### 2. 前端 UI

#### 频道详情页面增强

**位置**: 频道管理 → 选择频道 → 频道信息区域

**修改前**:
```
最大消息数: 无限制
```

**修改后**:
```
最大消息数: 无限制 [编辑]
              ↑
          点击此按钮
```

#### 交互流程

1. **点击"编辑"按钮**
   - 弹出对话框
   - 显示当前值
   - 提示 0 = 无限制

2. **输入新值**
   - 输入数字（如 100）
   - 或输入 0（无限制）

3. **确认保存**
   - 调用 API 更新
   - 显示成功提示
   - 自动刷新详情页

---

### 3. CSS 样式

新增了编辑按钮的样式：

```css
.value-edit {
  display: flex;
  align-items: center;
  gap: 8px;
}

.btn-xs {
  padding: 2px 8px;
  font-size: 11px;
  border-radius: 3px;
  /* ... */
}

.btn-xs:hover {
  background: var(--accent);
  color: white;
}
```

---

## 💡 使用示例

### 场景 1: 设置消息上限

```
步骤:
1. 进入"频道管理"视图
2. 点击频道名（如 "general"）
3. 在"频道信息"区域找到"最大消息数"
4. 点击"编辑"按钮
5. 输入: 100
6. 点击确定

结果:
✅ 频道最多保留 100 条消息
✅ 超过时自动删除最旧的消息
```

### 场景 2: 取消消息限制

```
步骤:
同上，但输入: 0

结果:
✅ 消息数无限制
✅ 不会自动删除消息
```

### 场景 3: 调整现有上限

```
当前: 50 条
目标: 200 条

步骤:
1. 点击"编辑"
2. 输入: 200
3. 确定

结果:
✅ 上限从 50 提升到 200
✅ 现有的 50 条消息保留
```

---

## 🔧 技术细节

### 数据存储

频道配置保存在 `{name}.meta.json` 文件中：

```json
{
  "name": "general",
  "members": ["worker1", "worker2"],
  "admins": [],
  "max_messages": 100,  // ← 这里
  "created_by": "",
  "created_at": ""
}
```

### Channel 类集成

```python
class Channel:
    def __init__(self, path, name, max_messages=0):
        self.max_messages = max_messages  # 实例属性
        
    # 更新时同时修改实例属性和 meta 文件
    def update_config(self, new_max):
        self.max_messages = new_max
        meta = self._load_meta()
        meta["max_messages"] = new_max
        self._save_meta(meta)
```

### 消息修剪逻辑

当频道消息数超过 `max_messages` 时：

```python
if self.max_messages > 0 and message_count > self.max_messages:
    # 删除最旧的多余消息
    excess = message_count - self.max_messages
    trim_old_messages(excess)
```

---

## 📊 API 使用示例

### cURL

```bash
# 设置最大消息数为 100
curl -X PUT http://127.0.0.1:8765/api/channels/general/config \
  -H "Content-Type: application/json" \
  -d '{"max_messages": 100}'

# 取消限制（设为 0）
curl -X PUT http://127.0.0.1:8765/api/channels/general/config \
  -H "Content-Type: application/json" \
  -d '{"max_messages": 0}'
```

### JavaScript

```javascript
// 更新最大消息数
async function updateChannelMaxMessages(channelName, maxMsgs) {
  const response = await fetch(`/api/channels/${channelName}/config`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ max_messages: maxMsgs })
  });
  
  const data = await response.json();
  console.log(`Updated ${data.channel}: max_messages = ${data.max_messages}`);
}

// 使用
updateChannelMaxMessages('general', 100);
```

### Python

```python
import requests

# 更新配置
response = requests.put(
    'http://127.0.0.1:8765/api/channels/general/config',
    json={'max_messages': 100}
)

print(response.json())
# {'ok': True, 'channel': 'general', 'max_messages': 100}
```

---

## ⚠️ 注意事项

### 1. 值为 0 的含义
- `max_messages = 0`: 无限制
- `max_messages > 0`: 限制为该值
- `max_messages < 0`: ❌ 无效，API 会拒绝

### 2. 即时生效
- 配置更新后立即生效
- 不需要重启 Worker
- 不需要刷新页面（会自动刷新）

### 3. 历史消息
- 调小上限时，超出的旧消息会被删除
- 调大上限时，现有消息保留
- 删除操作不可恢复

### 4. 权限控制
- 目前任何人都可以修改
- 未来可能需要 admin 权限检查

---

## 🎨 UI 截图说明

### 编辑前
```
┌─────────────────────────────┐
│ 频道信息                     │
├─────────────────────────────┤
│ 最大消息数: 无限制  [编辑]   │  ← 点击这里
│ 成员数: 2                    │
│ 管理员: 无                   │
└─────────────────────────────┘
```

### 编辑对话框
```
┌──────────────────────────────────┐
│ 设置频道 "general" 的最大消息数:  │
│ (当前: 无限制, 0 = 无限制)       │
│                                  │
│ [  100                        ]  │  ← 输入新值
│                                  │
│      [取消]     [确定]           │
└──────────────────────────────────┘
```

### 编辑后
```
┌─────────────────────────────┐
│ 频道信息                     │
├─────────────────────────────┤
│ 最大消息数: 100      [编辑]  │  ← 已更新
│ 成员数: 2                    │
│ 管理员: 无                   │
└─────────────────────────────┘
```

---

## 🚀 未来扩展

### 可能的改进

1. **批量设置**
   - 一次性设置多个频道的 max_messages
   - 应用模板配置

2. **预设选项**
   - 下拉菜单选择常用值（50, 100, 500, 无限制）
   - 减少手动输入

3. **实时预览**
   - 显示当前消息数
   - 预测删除多少消息

4. **审计日志**
   - 记录谁在何时修改了配置
   - 查看配置变更历史

5. **自动优化建议**
   - 根据消息增长速度推荐合适的上限
   - 智能提醒

---

## 📝 总结

### 实现的功能
- ✅ 后端 API 支持更新频道配置
- ✅ 前端 UI 提供便捷的编辑入口
- ✅ 友好的用户交互（prompt 对话框）
- ✅ 即时的视觉反馈
- ✅ 完善的错误处理

### 用户体验
- 🎯 操作简单：只需点击 + 输入
- ⚡ 即时生效：无需等待
- 💬 清晰提示：当前值和含义明确
- 🛡️ 安全可靠：输入验证 + 错误提示

### 代码质量
- 📦 模块化：独立的 API 端点
- 🧪 可测试：清晰的输入输出
- 📖 可维护：代码注释完整
- 🔄 可扩展：易于添加更多配置项

---

现在你可以轻松调整任何频道的最大消息数了！🎉
