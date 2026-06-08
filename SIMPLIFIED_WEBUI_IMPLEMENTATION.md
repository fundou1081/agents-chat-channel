# 简化版 WebUI 实现说明

## 🎯 设计目标

创建一个简洁直观的 WebUI，专注于：
1. **快速创建频道** - 一键创建新频道
2. **成员管理** - 添加/移除成员  
3. **实时状态监控** - 查看成员的当前状态和正在处理的 prompt

## ✅ 已完成的改进

### 1. 简化导航结构
- 移除了复杂的菜单项（邮箱、Sessions、任务板、统计）
- 保留核心功能：**频道管理**、**实时聊天**、**Workers**
- 默认视图改为"频道管理"

### 2. 新增 API 端点
```python
GET /api/channels/{name}/member-status
```

返回频道成员的实时状态：
```json
{
  "channel": "fish-market",
  "members": [
    {
      "agent_id": "seller-fish",
      "status": "processing",  // idle, processing, waiting
      "current_session": {
        "session_id": "s1",
        "topic": "买鱼讨价还价",
        "progress": 80,
        "next_action": "等待 buyer 回复"
      },
      "current_prompt": "你是卖鱼小贩...",
      "progress": 80,
      "last_activity": "2026-06-08T10:01:00Z"
    }
  ],
  "total_members": 2
}
```

### 3. UI 布局优化
- **频道管理视图**: 左右分栏布局
  - 左侧：频道列表
  - 右侧：频道详情 + 成员状态监控
- **实时聊天视图**: 简化输入框，专注观察
- **Workers 视图**: 基础 Worker 列表

## 📋 待实现功能

### 前端 JavaScript (`webui/app.js`)

需要添加以下函数：

```javascript
// 1. 刷新频道列表
async function refreshChannelList() {
  const chs = await api('/api/channels');
  // 渲染频道列表，显示成员数量
}

// 2. 加载频道详情和成员状态
async function loadChannelDetail(name) {
  const [meta, memberStatus] = await Promise.all([
    api(`/api/channels/${name}/meta`),
    api(`/api/channels/${name}/member-status`)
  ]);
  
  // 渲染频道信息
  // 渲染成员状态卡片
}

// 3. 渲染成员状态卡片
function renderMemberStatus(member) {
  return `
    <div class="member-status-card ${member.status}">
      <div class="member-header">
        <span class="member-name">${member.agent_id}</span>
        <span class="member-status-badge">${getStatusText(member.status)}</span>
      </div>
      ${member.current_session ? `
        <div class="session-info">
          <div class="session-topic">${member.current_session.topic}</div>
          <div class="progress-bar">
            <div class="progress-fill" style="width: ${member.progress}%"></div>
          </div>
          <div class="next-action">${member.current_session.next_action}</div>
        </div>
      ` : ''}
      ${member.current_prompt ? `
        <div class="prompt-preview">
          <details>
            <summary>查看当前 Prompt</summary>
            <pre>${escapeHtml(member.current_prompt)}</pre>
          </details>
        </div>
      ` : ''}
    </div>
  `;
}

// 4. 创建频道
async function createChannel(name, maxMessages = 0) {
  await api(`/api/channels/${name}/messages`, {
    method: 'POST',
    body: JSON.stringify({ 
      from: 'god', 
      content: 'init channel', 
      type: 'text' 
    })
  });
}

// 5. 添加成员到频道
async function addMemberToChannel(channelName, agentId) {
  await api(`/api/channels/${channelName}/members`, {
    method: 'POST',
    body: JSON.stringify({ agent_id: agentId })
  });
}
```

### CSS 样式 (`webui/style.css`)

需要添加以下样式：

```css
/* 频道管理布局 */
.channel-management-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  height: calc(100vh - 120px);
}

.channel-list-panel {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  overflow-y: auto;
}

.channel-detail-panel {
  background: var(--bg-secondary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 20px;
  overflow-y: auto;
}

/* 成员状态卡片 */
.member-status-card {
  background: var(--bg-tertiary);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 12px;
  transition: all 0.2s;
}

.member-status-card.processing {
  border-left: 4px solid var(--accent);
}

.member-status-card.idle {
  border-left: 4px solid var(--success);
}

.member-status-card.waiting {
  border-left: 4px solid var(--warning);
}

.member-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}

.member-name {
  font-weight: 700;
  font-size: 14px;
}

.member-status-badge {
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
}

.session-info {
  margin-top: 8px;
}

.session-topic {
  font-size: 13px;
  color: var(--text-primary);
  margin-bottom: 8px;
}

.progress-bar {
  height: 6px;
  background: var(--bg-primary);
  border-radius: 3px;
  overflow: hidden;
  margin-bottom: 8px;
}

.progress-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s;
}

.next-action {
  font-size: 12px;
  color: var(--text-secondary);
}

.prompt-preview {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

.prompt-preview details {
  font-size: 12px;
}

.prompt-preview pre {
  background: var(--bg-primary);
  padding: 8px;
  border-radius: 4px;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 200px;
  overflow-y: auto;
}
```

## 🚀 使用流程

### 1. 创建频道
1. 点击顶部 "+ 新建频道" 按钮
2. 输入频道名称
3. （可选）设置最大消息数
4. 点击"创建"

### 2. 添加成员
1. 选择频道
2. 在成员管理区域输入 agent_id
3. 点击"+ 添加成员"

### 3. 查看成员状态
1. 选择频道后自动显示成员列表
2. 每个成员显示：
   - 状态（空闲/处理中/等待）
   - 当前 Session 主题
   - 进度条
   - 下一步动作
   - 当前 Prompt（可展开查看）

### 4. 实时聊天
1. 切换到"实时聊天"视图
2. 选择要观察的频道
3. 实时查看消息流
4. 可在底部输入框发送消息

## 📝 注意事项

1. **Prompt 文件**: 需要在 Agent 执行时保存当前 prompt 到 `workspace/current_prompt.txt`
2. **实时更新**: 建议每 3-5 秒轮询一次成员状态
3. **性能优化**: 大量成员时使用虚拟滚动
4. **错误处理**:  gracefully 处理 API 失败情况

## 🎨 视觉设计要点

- **状态颜色**:
  - 处理中 (processing): 蓝色
  - 空闲 (idle): 绿色
  - 等待 (waiting): 黄色
  
- **进度条**: 直观显示任务完成度
- **卡片布局**: 清晰的层次结构
- **悬停效果**: 增强交互反馈

---

这个简化版设计聚焦于核心功能，让用户能够快速创建频道、管理成员，并实时监控他们的工作状态。
