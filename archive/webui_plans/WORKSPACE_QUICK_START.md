# Worker 创建快速指南

## 🚀 快速开始

### 访问 WebUI
打开浏览器: http://127.0.0.1:8765

### 创建 Worker
1. 点击左侧导航的 **🤖 Workers**
2. 点击右上角 **+ 新建 Worker** 按钮
3. 填写配置信息
4. 点击 **创建**

---

## 📝 两种创建模式

### 模式 1: 创建新 Workspace ⭐推荐新手

**适用场景:**
- 第一次创建某类 Worker
- 需要自定义角色和提示词
- 需要特定的 skills 配置

**步骤:**
```
1. 保持"创建新 Workspace"选中（默认）
2. 填写 Worker ID: my-worker
3. 选择 CLI 类型: mock（测试用）
4. （可选）填写角色名称: 卖鱼小贩
5. （可选）填写系统提示或留空使用默认
6. （可选）填写 skills: bargaining, negotiation
7. 点击创建
```

**结果:**
- ✅ 创建新的 workspace 目录
- ✅ 自动生成 roles.md（默认模板或自定义）
- ✅ 创建 mailbox 文件
- ✅ 完全独立的配置

---

### 模式 2: 使用已有 Workspace ⭐推荐批量部署

**适用场景:**
- 创建相同角色的多个实例
- 复用成熟的配置
- 快速部署标准化 Worker

**步骤:**
```
1. 切换到"使用已有 Workspace"
2. 从下拉框选择已有的 workspace
3. 查看 workspace 信息确认
4. 填写新的 Worker ID: my-worker-2
5. 点击创建
```

**结果:**
- ✅ 复用选中的 workspace 配置
- ✅ 共享 roles.md 和 skills
- ✅ 创建独立的 mailbox
- ⚠️ 注意：修改 roles.md 会影响所有使用者

---

## 🎯 运行模式选择

### Passive 模式（默认）
```
特点:
- 等待被 @mention 才响应
- 适合被动参与对话
- 节省资源

示例:
@seller-fish 这条鱼多少钱？
→ seller-fish 收到邮件并回复
```

### Proactive 模式
```
特点:
- 主动订阅频道
- 定期轮询新消息
- 适合监控和自动响应

步骤:
1. 选择"Proactive (订阅频道)"
2. 填写订阅频道: general, fish-market
3. 创建后 Worker 会自动监听这些频道
```

---

## 💡 实用技巧

### 技巧 1: 快速创建测试 Worker
```
Worker ID: test-1
CLI 类型: Mock
模式: Passive
Workspace: 创建新（全部留空）
→ 10秒内完成，使用默认配置
```

### 技巧 2: 批量创建相同角色
```
第1次:
- 创建 "seller-fish"，配置好 roles.md
- 测试满意

第2次:
- 创建 "seller-fish-2"
- 选择"使用已有 Workspace" → seller-fish
- 立即获得相同配置
```

### 技巧 3: 查看 Workspace 详情
```
1. 选择"使用已有 Workspace"
2. 从下拉框选择一个 workspace
3. 下方自动显示:
   - 路径位置
   - 文件数量
   - 是否有角色配置 ✓/✗
   - 是否有订阅配置 ✓/✗
```

### 技巧 4: 配置 Skills
```
Skills 输入格式:
bargaining, negotiation, price-analysis

效果:
- 在 workspace/skills/ 目录创建软链接
- 指向全局 skills 目录
- Worker 可以加载这些技能
```

---

## 🔍 常见问题

### Q1: 什么时候应该创建新 Workspace？
**A:** 
- ✅ Worker 有独特的角色
- ✅ 需要不同的 system prompt
- ✅ 第一次尝试某类 Worker
- ❌ 不要为每个测试都创建新 workspace

### Q2: 什么时候应该复用 Workspace？
**A:**
- ✅ 创建相同角色的多个实例
- ✅ 已经测试满意的配置
- ✅ 需要标准化部署
- ❌ 不要复用还在调试的配置

### Q3: CLI 类型怎么选？
**A:**
- **Mock**: 测试用，不消耗 API，返回固定响应
- **OpenCode**: 生产环境，使用 OpenAI 兼容 API
- **Qwen**: 生产环境，使用阿里云通义千问

### Q4: System Prompt 留空会怎样？
**A:**
会使用默认模板，包含 5 条基本规则：
1. 只回应 @提及你的消息
2. 保持简洁专业的语气
3. 一次只做一件事
4. 不确定时询问澄清
5. 遵循频道规则

### Q5: 创建后可以修改配置吗？
**A:**
- **roles.md**: 可以直接编辑 workspace/roles.md
- **subscriptions**: 编辑 workspace/subscriptions.json
- **mailbox**: 通过 API 或手动编辑 JSON
- 修改后需要重启 Worker 生效

---

## ⚠️ 注意事项

### 1. Worker ID 唯一性
```
❌ 错误: 创建两个相同的 ID
✅ 正确: 每个 Worker 有唯一的 ID
```

### 2. Workspace 共享风险
```
如果 3 个 Worker 共享同一个 workspace:
- 修改 roles.md 会影响所有 3 个
- 建议: 共享前先备份，或使用独立 workspace
```

### 3. Proactive 模式资源消耗
```
Proactive Worker 会定期轮询频道:
- 订阅太多频道会消耗资源
- 建议: 只订阅必要的频道
- 监控: 查看 worker 日志了解活动情况
```

### 4. 文件权限
```
确保 data_v2 目录可写:
ls -la data_v2/workspaces/
→ 应该有读写权限
```

---

## 📊 配置示例

### 示例 1: 简单的测试 Worker
```json
{
  "agent_id": "test-bot",
  "cli_type": "mock",
  "mode": "passive",
  "workspace": "new"
}
```

### 示例 2: 生产环境的卖鱼小贩
```json
{
  "agent_id": "seller-fish-prod",
  "cli_type": "opencode",
  "mode": "passive",
  "workspace": "new",
  "role": "卖鱼小贩",
  "system_prompt": "你是经验丰富的卖鱼小贩...",
  "skills": ["bargaining", "fish-knowledge"]
}
```

### 示例 3: 新闻监控机器人
```json
{
  "agent_id": "news-monitor",
  "cli_type": "qwen",
  "mode": "proactive",
  "workspace": "new",
  "subscriptions": ["general", "news", "announcements"],
  "role": "新闻监控员",
  "system_prompt": "你负责监控新闻并摘要..."
}
```

### 示例 4: 批量部署客服
```json
// 第1个
{
  "agent_id": "support-1",
  "workspace": "new",
  "role": "客服代表",
  ...
}

// 第2-10个
{
  "agent_id": "support-2",
  "workspace": "existing",
  "existing_workspace_name": "support-1",
  ...
}
```

---

## 🎉 总结

| 场景 | 推荐模式 | Workspace |
|------|---------|-----------|
| 首次尝试 | Passive + Mock | 创建新 |
| 生产部署 | Passive/Proactive + OpenCode/Qwen | 创建新 |
| 批量复制 | 与原 Worker 相同 | 使用已有 |
| 快速测试 | Passive + Mock | 创建新（留空） |

记住核心原则：
- **独特角色** → 创建新 Workspace
- **相同角色** → 复用已有 Workspace
- **测试阶段** → 用 Mock CLI
- **生产环境** → 用 OpenCode/Qwen

祝你创建 Worker 愉快！🚀
