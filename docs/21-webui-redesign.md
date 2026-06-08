# WebUI v2.0 重新设计方案

## 🎯 设计理念

基于 v2.0 PDR 架构重新设计 WebUI，使其与系统架构完全对齐。

## 📊 核心视图设计

### 1. **总览 Dashboard** 
显示系统整体状态：
- 活跃 Worker 数量
- 频道消息统计
- 任务状态板概览
- 文件总线健康状态

### 2. **Worker 监控中心** ⭐ 核心
每个 Worker 的 PDR 四组件状态：

```
┌─────────────────────────────────────────────────────────────┐
│ Worker: seller-fish                                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  📡 Perceive (CommunicationComponent)                      │
│  ├── 邮箱待处理: 3 封                                       │
│  ├── 订阅频道: fish-market, general                         │
│  └── 最后轮询: 2s 前                                        │
│                                                             │
│  🧠 Decide (EventHandler + DecisionMaker)                   │
│  ├── 运行模式: proactive                                    │
│  ├── 当前决策: speak (理由: 有新消息需要回复)                │
│  └── 决策历史: [最近5次决策]                                │
│                                                             │
│  💾 Remember (SessionManager)                               │
│  ├── 活跃 Sessions: 2                                       │
│  │   ├── s1: 买鱼讨价还价 (progress: 80%)                  │
│  │   └── s2: 市场行情咨询 (progress: 30%)                  │
│  └── Session 快照: [查看完整状态]                           │
│                                                             │
│  ⚡ Act (CLI Client)                                        │
│  ├── CLI 类型: OpenCodeCLI                                  │
│  ├── Model: minimax-m3-free                                 │
│  ├── Workspace: /data/workspaces/seller-fish               │
│  └── 最后执行: 5s 前                                        │
│                                                             │
│  📋 操作按钮                                                │
│  [启动] [停止] [查看日志] [清空邮箱] [查看Workspace]        │
└─────────────────────────────────────────────────────────────┘
```

### 3. **实时聊天观察** 🔴
改进版实时聊天界面：

**功能特性**：
- 多频道同时观察（标签页切换）
- 消息来源标识（god/worker/system）
- STATUS 块高亮显示
- @mention 路由追踪
- 参与互动（发送消息）

**消息显示**：
```
┌─────────────────────────────────────────────────────────────┐
│ [10:01] seller-fish (worker)                                │
│ @buyer-fish 100块一斤,这品质绝对值                           │
│                                                             │
│ [STATUS] 报价100元 | 下一步: 等buyer还价                     │
│ ↑ STATUS 块高亮显示                                          │
└─────────────────────────────────────────────────────────────┘
```

### 4. **频道管理**
- 频道列表（JSONL 文件）
- 成员管理（members/admins）
- 消息历史查看
- 频道元数据编辑

### 5. **Session 管理器**
- 按 Worker 分组的 Sessions
- Session 详情（topic/progress/next_action）
- Session 快照查看
- Session 生命周期追踪

### 6. **任务状态板 (State Board)**
- 全局任务列表
- 任务进度可视化
- 锁状态监控
- Stale Task 检测

### 7. **文件总线浏览器** 🔍
直接浏览底层文件结构：
```
data_v2/
├── channels/
│   ├── fish-market.jsonl [查看] [下载]
│   └── general.jsonl [查看] [下载]
├── mailboxes/
│   ├── seller-fish.json [查看] [清空]
│   └── buyer-fish.json [查看] [清空]
├── sessions/
│   ├── seller-fish.json [查看]
│   └── buyer-fish.json [查看]
├── locks/
│   └── task_001.lock [查看] [释放]
├── state_board.json [查看]
├── scanner_state.json [查看]
└── scheduler_state.json [查看]
```

### 8. **Scanner 监控**
- Scanner 进程状态
- 各频道扫描 offset
- 邮件投递统计
- 模糊匹配日志

### 9. **Scheduler 监控**
- Scheduler 进程状态
- Stale Task 检测
- 锁超时监控
- Request Status 邮件记录

### 10. **统计分析**
- Worker 活跃度
- 消息吞吐量
- Session 完成率
- CLI 调用统计

## 🎨 UI/UX 改进

### 1. **响应式布局**
- 桌面端：多列网格布局
- 平板端：双列布局
- 手机端：单列布局

### 2. **实时更新**
- WebSocket 连接（替代轮询）
- 增量更新（只更新变化的部分）
- 离线缓存

### 3. **交互优化**
- 拖拽排序
- 快捷键支持
- 批量操作
- 搜索过滤

### 4. **视觉设计**
- 深色主题（保持现有风格）
- 状态颜色编码
- 动画过渡效果
- 图标系统

## 🔧 技术实现

### 前端技术栈
```javascript
// 建议升级到现代前端框架
- React/Vue.js (可选，保持纯 JS 也可)
- WebSocket 实时通信
- LocalStorage 缓存
- Service Worker (PWA 支持)
```

### API 端点扩展
```python
# 新增 API 端点
GET  /api/workers/{id}/pdr-status    # PDR 四组件状态
GET  /api/workers/{id}/sessions      # Session 列表
GET  /api/workers/{id}/snapshot      # Session 快照
GET  /api/scanner/status             # Scanner 状态
GET  /api/scheduler/status           # Scheduler 状态
GET  /api/files/browse               # 文件浏览器
POST /api/files/{path}/view          # 查看文件内容
POST /api/locks/{id}/release         # 释放锁
WS   /ws/realtime                    # WebSocket 实时推送
```

### 数据结构优化
```python
# Worker PDR 状态
{
  "agent_id": "seller-fish",
  "mode": "proactive",
  "pdr": {
    "perceive": {
      "pending_mails": 3,
      "subscriptions": ["fish-market"],
      "last_poll": "2026-06-08T10:00:00Z"
    },
    "decide": {
      "last_decision": "speak",
      "decision_reason": "有新消息需要回复",
      "decision_history": [...]
    },
    "remember": {
      "active_sessions": 2,
      "session_snapshots": [...]
    },
    "act": {
      "cli_type": "OpenCodeCLI",
      "model": "minimax-m3-free",
      "workspace_dir": "...",
      "last_execution": "..."
    }
  }
}
```

## 📋 实施计划

### Phase 1: 核心功能 (1-2天)
- [ ] 重构 Worker 监控中心（PDR 四组件）
- [ ] 改进实时聊天界面
- [ ] 添加 Session 管理器
- [ ] 添加 State Board 可视化

### Phase 2: 高级功能 (2-3天)
- [ ] 文件总线浏览器
- [ ] Scanner/Scheduler 监控
- [ ] 统计分析面板
- [ ] WebSocket 实时推送

### Phase 3: 优化完善 (1-2天)
- [ ] 响应式设计
- [ ] 性能优化
- [ ] 用户体验改进
- [ ] 文档更新

## 🎯 关键改进点

1. **架构对齐**: UI 结构与 PDR 四组件完全对应
2. **实时监控**: WebSocket 替代轮询，更低延迟
3. **深度洞察**: 暴露底层文件总线状态
4. **调试友好**: 直接查看和操作系统文件
5. **模式区分**: 清晰展示 passive/proactive 模式差异

## 📝 注意事项

1. **向后兼容**: 保留现有 API，逐步迁移
2. **性能考虑**: 大量数据时分页加载
3. **安全性**: 文件访问权限控制
4. **可维护性**: 模块化代码结构

---

这个设计方案确保 WebUI 与 v2.0 PDR 架构完全对齐，提供更深度的系统洞察和更好的调试体验。
