# Worker 创建与 Workspace 管理功能

## 🎯 功能概述

增强了 Worker 创建流程，支持两种模式：
1. **创建新 Workspace** - 从零开始配置新的工作空间
2. **使用已有 Workspace** - 复用现有的 workspace 配置

## ✅ 已实现的功能

### 1. 后端 API

#### 列出所有 Workspaces
```python
GET /api/workspaces

Response:
{
  "workspaces": [
    {
      "name": "seller-fish",
      "path": "/path/to/data_v2/workspaces/seller-fish",
      "has_roles": true,
      "cli_type": "mock",
      "created_at": 1717843200.0
    }
  ]
}
```

#### 创建 Worker（支持选择 Workspace）
```python
POST /api/agents/{agent_id}/create

Request Body:
{
  "mode": "passive" | "proactive",
  "cli_type": "mock" | "opencode" | "qwen",
  
  // 选项 1: 创建新 workspace
  "use_existing_workspace": false,
  "role": "卖鱼小贩",
  "system_prompt": "你是...",
  "skills": ["bargaining", "negotiation"],
  
  // 选项 2: 使用已有 workspace
  "use_existing_workspace": true,
  "existing_workspace_name": "seller-fish",
  
  // Proactive 模式额外参数
  "subscriptions": ["general", "fish-market"]
}

Response:
{
  "ok": true,
  "agent_id": "my-worker",
  "workspace": "/path/to/workspace",
  "used_existing": false
}
```

### 2. 前端 UI

#### 增强的创建 Worker 模态框

**基本信息区域:**
- Worker ID（必填）
- CLI 类型选择（Mock/OpenCode/Qwen）
- 运行模式（Passive/Proactive）

**Workspace 配置区域:**
- 单选切换：创建新 Workspace / 使用已有 Workspace

**创建新 Workspace 选项:**
- 角色名称（可选）
- 系统提示（可选，留空使用默认模板）
- Skills 列表（逗号分隔，可选）

**使用已有 Workspace 选项:**
- 下拉选择器（动态加载可用 workspaces）
- Workspace 详细信息展示：
  - 路径
  - 文件数量
  - 是否有角色配置
  - 是否有订阅配置

**Proactive 模式额外选项:**
- 订阅频道列表（逗号分隔）

### 3. 交互逻辑

#### 智能显示/隐藏
- 选择"使用已有 Workspace"时，隐藏新 workspace 选项
- 选择"创建新 Workspace"时，隐藏已有 workspace 选项
- 选择"Proactive"模式时，显示订阅频道输入框
- 选择"Passive"模式时，隐藏订阅频道输入框

#### 实时信息展示
- 选择已有 workspace 后，自动加载并显示详细信息
- 包括文件数量、配置状态等
- 帮助用户判断是否适合复用

#### 表单验证
- Worker ID 必填
- 使用已有 workspace 时必须选择
- 自动清理空值和多余空格

## 📊 工作流程

### 场景 1: 创建全新 Worker

```
步骤:
1. 点击 "+ 新建 Worker"
2. 填写 Worker ID: "buyer-apple"
3. 选择 CLI 类型: "mock"
4. 选择模式: "passive"
5. 保持"创建新 Workspace"选中
6. （可选）填写角色名称: "苹果买家"
7. （可选）填写系统提示或 skills
8. 点击"创建"

结果:
- 创建新的 workspace: data_v2/workspaces/buyer-apple
- 初始化 roles.md（使用默认模板或自定义内容）
- 创建 mailbox: data_v2/mailboxes/buyer-apple.json
- 返回成功消息
```

### 场景 2: 复用已有 Workspace

```
步骤:
1. 点击 "+ 新建 Worker"
2. 填写 Worker ID: "buyer-apple-2"
3. 选择 CLI 类型: "mock"
4. 选择模式: "passive"
5. 切换到"使用已有 Workspace"
6. 从下拉框选择: "buyer-apple"
7. 查看 workspace 信息确认
8. 点击"创建"

结果:
- 复用 buyer-apple 的 workspace 配置
- 创建新的 mailbox: data_v2/mailboxes/buyer-apple-2.json
- 两个 worker 共享相同的 roles.md 和 skills
- 适合创建相同角色的多个实例
```

### 场景 3: 创建 Proactive Worker

```
步骤:
1. 点击 "+ 新建 Worker"
2. 填写 Worker ID: "news-bot"
3. 选择 CLI 类型: "opencode"
4. 选择模式: "proactive"
5. 出现"订阅频道"输入框
6. 填写: "general, news, announcements"
7. 点击"创建"

结果:
- 创建 workspace 和 mailbox
- 自动设置订阅: data_v2/workspaces/news-bot/subscriptions.json
- Worker 启动后会自动监听这些频道
```

## 🎨 UI/UX 设计

### 视觉层次
1. **基本信息**: 顶部，必需字段
2. **Workspace 配置**: 中间，核心配置区域
   - 灰色背景区分
   - Radio 按钮清晰切换
3. **模式选项**: 底部，条件显示

### 交互反馈
- **Radio 切换**: 平滑过渡，立即显示对应选项
- **下拉选择**: 实时加载 workspace 列表
- **信息显示**: 选择后立即展示详情
- **表单提交**: 清晰的错误提示和成功消息

### 响应式设计
- 模态框宽度增加到 700px（modal-large 类）
- 内部元素自适应布局
- 移动端友好

## 🔧 技术实现要点

### 1. Workspace 初始化逻辑

```python
if use_existing_workspace and existing_workspace_name:
    # 复用已有 workspace
    ws_dir = data_dir / "workspaces" / existing_workspace_name
else:
    # 创建新 workspace
    ws_dir = data_dir / "workspaces" / agent_id
    _init_workspace(
        workspace_dir=ws_dir,
        cli_name=cli_type,
        role=role or agent_id,
        system_prompt=system_prompt,
        skills=skills,
        mcp_servers=mcp_servers,
        use_default_prompt=True,
    )
```

### 2. 前端状态管理

```javascript
let payload = {
  mode: mode,
  cli_type: cliType
};

if (workspaceType === 'existing') {
  payload.use_existing_workspace = true;
  payload.existing_workspace_name = existingWs;
} else {
  payload.role = role;
  payload.system_prompt = systemPrompt;
  payload.skills = skillsArray;
}
```

### 3. 动态加载优化

```javascript
// 打开模态框时才加载 workspace 列表
function showNewWorkerModal() { 
  loadWorkspaceList();
  // ...
}

// 缓存避免重复请求
async function loadWorkspaceList() {
  const data = await api('/api/workspaces');
  // 渲染下拉框...
}
```

## 💡 使用建议

### 何时创建新 Workspace？
- ✅ Worker 有独特的角色定位
- ✅ 需要自定义 system prompt
- ✅ 需要特定的 skills 配置
- ✅ 第一次创建某类 Worker

### 何时复用已有 Workspace？
- ✅ 创建相同角色的多个实例
- ✅ 测试不同配置的效果
- ✅ 快速部署标准化 Worker
- ✅ 节省配置时间

### 最佳实践
1. **命名规范**: workspace 名称应反映角色功能
2. **Skills 管理**: 将常用 skills 组合成模板
3. **System Prompt**: 保持简洁明确，便于复用
4. **版本控制**: 重要的 workspace 配置应备份

## 🚀 扩展可能性

### 未来改进方向
1. **Workspace 模板**: 预定义常用配置模板
2. **批量创建**: 一次性创建多个 Worker
3. **Workspace 克隆**: 复制并修改现有配置
4. **导入/导出**: 支持 workspace 配置的导入导出
5. **可视化编辑**: GUI 编辑 roles.md 和 skills
6. **配置验证**: 检查 workspace 配置的完整性

## ⚠️ 注意事项

### 1. Workspace 共享风险
- 多个 Worker 共享同一 workspace 时
- 对 roles.md 的修改会影响所有使用者
- 建议只读共享，或使用独立的 workspace

### 2. 文件权限
- 确保 data_v2/workspaces 目录可写
- Worker 进程需要有读写权限

### 3. 配置冲突
- 使用已有 workspace 时不会覆盖现有文件
- roles.md 如果已存在会保留原内容
- subscriptions.json 会被新值覆盖

### 4. 性能考虑
- 大量 workspace 时加载可能稍慢
- 建议定期清理未使用的 workspace
- 可以考虑分页加载

---

这个增强的 Worker 创建功能提供了灵活的配置选项，既支持快速创建标准 Worker，也支持精细化的自定义配置，大大提升了多智能体系统的易用性！
