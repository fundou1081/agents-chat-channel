// agents-chat-channel v2.0 · WebUI JavaScript
//
// 设计理念:
// - 频道 = Team (一个频道就是一个独立 team)
// - Worker = Agent (每个 worker 扮演一个角色)
// - 顶部 Channel 切换器: 用户在多 team 间快速切换
// - 6 个视图: 总览 / 频道详情 / 实时聊天 / Workers / 任务 / 邮箱

const API = '';

const state = {
  currentView: 'overview',
  currentChannel: '',  // 当前选中的频道 (顶部 selector)
  refreshTimer: null,
  refreshInterval: 5000,
  liveChatTimer: null,
  lastMessageId: null,
  liveChatChannel: '',
  activeWorkerDetail: '',  // modal 中正在查看的 worker
};

// ============================
// 工具函数
// ============================

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    const err = await res.text().catch(() => res.statusText);
    throw new Error(`${path}: ${res.status} ${err}`);
  }
  return res.json();
}

function $(id) { return document.getElementById(id); }
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function showToast(msg, type = 'info', duration = 3000) {
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast ' + type;
  setTimeout(() => t.classList.add('hidden'), duration);
}

function fmtTime(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour12: false });
  } catch { return String(iso).slice(0, 8); }
}

function fmtRelTime(iso) {
  if (!iso) return '';
  try {
    const diff = (Date.now() - new Date(iso)) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return `${Math.floor(diff / 60)}m前`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h前`;
    return `${Math.floor(diff / 86400)}d前`;
  } catch { return ''; }
}

// ============================
// 顶部 Channel 选择器 (核心: 多 team 切换)
// ============================

async function refreshChannelSelector() {
  const sel = $('current-channel-select');
  if (!sel) return;
  try {
    const chs = await api('/api/channels');
    const cur = state.currentChannel;
    sel.innerHTML = '<option value="">— 全部频道 —</option>' +
      chs.map(c => `<option value="${c.name}" ${cur === c.name ? 'selected' : ''}>${c.name}</option>`).join('');
  } catch (e) { /* silent */ }
}

$('current-channel-select')?.addEventListener('change', (e) => {
  state.currentChannel = e.target.value;
  state.liveChatChannel = state.currentChannel;  // 同步到 live chat
  refresh();  // 刷新当前视图
});

// ============================
// 导航
// ============================

document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => switchView(el.dataset.view));
});

function switchView(view) {
  state.currentView = view;
  document.querySelectorAll('.nav-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === view);
  });
  document.querySelectorAll('.view').forEach(el => {
    el.classList.toggle('active', el.id === 'view-' + view);
  });
  // 切换视图时停止 live chat polling
  if (view !== 'live-chat') stopLiveChatRefresh();
  refresh();
}

// ============================
// 自动刷新
// ============================

$('auto-refresh')?.addEventListener('change', e => {
  if (e.target.checked) startAutoRefresh();
  else stopAutoRefresh();
});

$('refresh-btn')?.addEventListener('click', () => { refresh(); });
$('refresh-interval')?.addEventListener('change', e => {
  state.refreshInterval = parseInt(e.target.value);
  if ($('auto-refresh')?.checked) {
    stopAutoRefresh();
    startAutoRefresh();
  }
});

function startAutoRefresh() {
  stopAutoRefresh();
  state.refreshTimer = setInterval(refresh, state.refreshInterval);
}

function stopAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

// ============================
// 主刷新
// ============================

async function refresh() {
  try {
    await api('/api/health');
    setConnStatus(true);
  } catch (e) {
    setConnStatus(false, e.message);
    return;
  }

  // 总是刷新 channel 选择器 (让用户能切换 team)
  await refreshChannelSelector();

  // 根据当前 view 刷新内容
  switch (state.currentView) {
    case 'overview': await refreshOverview(); break;
    case 'channel': await refreshChannelDetail(); break;
    case 'live-chat': startLiveChatRefresh(); break;
    case 'workers': await refreshWorkers(); break;
    case 'tasks': await refreshTasks(); break;
    case 'mailboxes': await refreshMailboxes(); break;
  }

  await refreshBadges();
}

function setConnStatus(ok, msg = '') {
  const el = $('conn-status');
  const txt = $('conn-text');
  if (el) el.className = 'conn-status ' + (ok ? 'ok' : 'err');
  if (txt) txt.textContent = ok ? '已连接' : (msg || '连接失败');
}

async function refreshBadges() {
  try {
    const chs = await api('/api/channels');
    const ags = await api('/api/agents');
    const bd = await api('/api/state_board');
    const tasks = bd.tasks || {};

    const bch = $('badge-channels');
    const bw = $('badge-workers');
    const bt = $('badge-tasks');
    if (bch) bch.textContent = chs.length || '';
    if (bw) bw.textContent = ags.length || '';
    if (bt) bt.textContent = Object.keys(tasks).length || '';
  } catch (e) { /* silent */ }
}

// ============================
// 总览 (Overview)
// ============================

async function refreshOverview() {
  const el = $('overview-content');
  if (!el) return;

  try {
    const [chs, ags, bd, sb] = await Promise.all([
      api('/api/channels'),
      api('/api/agents'),
      api('/api/state_board'),
      api('/api/stats'),
    ]);

    const tasks = bd.tasks || {};
    const taskCount = Object.keys(tasks).length;
    const totalPending = ags.reduce((s, a) => s + (a.pending || 0), 0);

    el.innerHTML = `
      <div class="card-grid">
        <div class="card" onclick="switchView('channel')">
          <div class="card-icon">💬</div>
          <div class="card-label">频道 (Teams)</div>
          <div class="card-value">${chs.length}</div>
        </div>
        <div class="card" onclick="switchView('workers')">
          <div class="card-icon">🤖</div>
          <div class="card-label">Workers</div>
          <div class="card-value">${ags.length}</div>
          <div class="card-sub">${totalPending} 待处理邮件</div>
        </div>
        <div class="card" onclick="switchView('tasks')">
          <div class="card-icon">📋</div>
          <div class="card-label">任务</div>
          <div class="card-value">${taskCount}</div>
        </div>
        <div class="card" onclick="switchView('mailboxes')">
          <div class="card-icon">📥</div>
          <div class="card-label">总邮件</div>
          <div class="card-value">${totalPending}</div>
        </div>
      </div>

      <div class="section">
        <h3>📺 所有频道 (Teams)</h3>
        <div class="team-grid">
          ${chs.map(ch => `
            <div class="team-card" onclick="selectAndViewChannel('${ch.name}')">
              <div class="team-header">
                <div class="team-name">💬 ${escapeHtml(ch.name)}</div>
                <div class="team-counts">
                  <span class="count-badge">${ch.messages || 0} 消息</span>
                </div>
              </div>
              <div class="team-meta">
                ${ch.admins?.length ? `<div class="meta-line">👤 admins: ${ch.admins.join(', ')}</div>` : ''}
                ${ch.members?.length ? `<div class="meta-line">👥 members: ${ch.members.length} 个</div>` : ''}
                ${ch.max_messages > 0 ? `<div class="meta-line">📊 限 ${ch.max_messages} 条</div>` : ''}
                ${ch.enabled_workers?.length ? `<div class="meta-line">🎯 白名单: ${ch.enabled_workers.join(', ')}</div>` : ''}
              </div>
              <div class="team-actions">
                <button class="btn btn-sm" onclick="event.stopPropagation(); switchViewChannel('${ch.name}')">📂 详情</button>
                <button class="btn btn-sm" onclick="event.stopPropagation(); switchViewLiveChat('${ch.name}')">🔴 实时</button>
              </div>
            </div>
          `).join('')}
          ${chs.length === 0 ? '<div class="empty-state">还没有频道. <button class="btn btn-primary" onclick="showNewChannelModal()">+ 创建第一个</button></div>' : ''}
        </div>
      </div>

      <div class="section">
        <h3>🤖 Workers 状态</h3>
        <div class="worker-mini-grid">
          ${ags.map(a => `
            <div class="worker-mini" onclick="showWorkerDetail('${a.agent_id}')">
              <div class="worker-mini-name">${escapeHtml(a.agent_id)}</div>
              <div class="worker-mini-status ${a.pending > 0 ? 'busy' : 'idle'}">
                ${a.pending > 0 ? `📬 ${a.pending}` : '🟢 空闲'}
              </div>
            </div>
          `).join('')}
          ${ags.length === 0 ? '<div class="empty-state">还没有 Worker. <button class="btn" onclick="showNewWorkerModal()">+ 创建</button></div>' : ''}
        </div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// 点击 team 卡片时切换顶部 selector 并跳到频道详情
window.selectAndViewChannel = (name) => {
  state.currentChannel = name;
  $('current-channel-select').value = name;
  switchView('channel');
};

window.switchViewChannel = (name) => {
  state.currentChannel = name;
  $('current-channel-select').value = name;
  switchView('channel');
};

window.switchViewLiveChat = (name) => {
  state.currentChannel = name;
  state.liveChatChannel = name;
  $('current-channel-select').value = name;
  switchView('live-chat');
};

// ============================
// 频道详情
// ============================

async function refreshChannelDetail() {
  const el = $('channel-detail-content');
  if (!el) return;

  const name = state.currentChannel;
  $('channel-title').textContent = name ? `· ${name}` : '';

  if (!name) {
    el.innerHTML = '<div class="empty-state">← 从顶部下拉框选择频道, 或 <button class="btn btn-sm btn-primary" onclick="showNewChannelModal()">+ 新建频道</button></div>';
    $('channel-actions-header').innerHTML = '';
    return;
  }

  try {
    const [meta, msgs, ags, memberStatus] = await Promise.all([
      api(`/api/channels/${name}/meta`),
      api(`/api/channels/${name}/messages?limit=50`),
      api('/api/agents'),
      api(`/api/channels/${name}/member-status`).catch(() => null),
    ]);

    const admins = meta.admins || [];
    const members = meta.members || [];
    const enabledWorkers = meta.enabled_workers || [];

    // 频道头
    $('channel-actions-header').innerHTML = `
      <button class="btn btn-sm" onclick="showChannelSettings('${name}')">⚙️ 设置</button>
      <button class="btn btn-sm btn-warn" onclick="clearChannelMessages()">🗑 清空</button>
    `;

    el.innerHTML = `
      <div class="channel-meta-info">
        <div class="meta-row">
          <span class="meta-label">admins:</span>
          <span>${admins.map(a => `<span class="tag">${escapeHtml(a)}</span>`).join('') || '—'}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">members:</span>
          <span>${members.length} 个 · ${members.map(m => `<span class="tag">${escapeHtml(m)}</span>`).join('') || '—'}</span>
        </div>
        ${enabledWorkers.length ? `
        <div class="meta-row">
          <span class="meta-label">白名单:</span>
          <span>${enabledWorkers.map(w => `<span class="tag tag-warn">${escapeHtml(w)}</span>`).join('')}</span>
        </div>` : ''}
        <div class="meta-row">
          <span class="meta-label">max_messages:</span>
          <span>${meta.max_messages || '不限制'}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">消息数:</span>
          <span><strong>${msgs.length}</strong> 条</span>
        </div>
      </div>

      ${memberStatus ? `
      <div class="section">
        <h3>🟢 成员实时状态</h3>
        <div class="member-status-grid">
          ${(memberStatus.members || []).map(m => `
            <div class="member-status-card ${m.status || 'idle'}">
              <div class="member-name">${escapeHtml(m.agent_id)}</div>
              <div class="member-status-badge">${escapeHtml(m.status || 'idle')}</div>
              ${m.current_session ? `
                <div class="member-session">
                  <div class="session-topic">${escapeHtml(m.current_session.topic || '')}</div>
                  <div class="progress-bar"><div class="progress-fill" style="width: ${m.progress || 0}%"></div></div>
                </div>
              ` : ''}
            </div>
          `).join('')}
        </div>
      </div>
      ` : ''}

      <div class="section">
        <h3>👥 成员管理</h3>
        <div class="member-control-row">
          <select id="add-member-select">
            <option value="">选择 worker 添加...</option>
            ${ags.filter(a => !members.includes(a.agent_id)).map(a => `<option value="${a.agent_id}">${a.agent_id}</option>`).join('')}
          </select>
          <button class="btn btn-sm" onclick="addMemberToCurrentChannel()">+ 成员</button>
          <select id="add-admin-select">
            <option value="">设为管理员...</option>
            ${ags.filter(a => !admins.includes(a.agent_id)).map(a => `<option value="${a.agent_id}">${a.agent_id}</option>`).join('')}
          </select>
          <button class="btn btn-sm" onclick="addAdminToCurrentChannel()">+ 管理员</button>
        </div>
        <div class="member-list">
          ${members.map(m => `
            <span class="member-chip">
              ${escapeHtml(m)}
              <button class="chip-remove" onclick="removeMemberFromCurrentChannel('${m}')" title="移除">×</button>
            </span>
          `).join('')}
        </div>
      </div>

      <div class="section">
        <h3>💬 最近消息 (${msgs.length} 条)</h3>
        <div class="chat-messages" id="channel-chat-messages">
          ${msgs.map(m => renderMessage(m)).join('')}
          ${msgs.length === 0 ? '<div class="empty-state">暂无消息</div>' : ''}
        </div>
      </div>

      <div class="section">
        <h3>✉️ 发送消息</h3>
        <div class="send-form">
          <div class="form-row inline">
            <input type="text" id="ch-msg-from" value="god" placeholder="发送者">
            <input type="text" id="ch-msg-mentions" placeholder="@提及 (逗号分隔, 留空不提及)">
          </div>
          <div class="form-row">
            <select id="ch-msg-type">
              <option value="mention">mention</option>
              <option value="task_broadcast">task_broadcast</option>
              <option value="text">text</option>
            </select>
          </div>
          <div class="form-row">
            <textarea id="ch-msg-content" rows="2" placeholder="消息内容... (Enter 发送, Shift+Enter 换行)"></textarea>
          </div>
          <button class="btn btn-primary" onclick="sendChannelMessage()">发送</button>
        </div>
      </div>
    `;

    // 自动滚动到底部
    const cm = $('channel-chat-messages');
    if (cm) cm.scrollTop = cm.scrollHeight;

    // Enter 键发送
    setTimeout(() => {
      const ta = $('ch-msg-content');
      if (ta) {
        ta.addEventListener('keydown', e => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendChannelMessage();
          }
        });
      }
    }, 100);
  } catch (e) {
    el.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

function renderMessage(m) {
  const isSelf = m.from === 'god';
  const isSystem = m.type === 'system' || m.type === 'status_report';
  return `<div class="chat-msg ${isSelf ? 'msg-self' : ''} ${isSystem ? 'msg-system' : ''}">
    <div class="chat-bubble">
      ${!isSelf ? `<div class="chat-sender">${escapeHtml(m.from || '')}</div>` : ''}
      <div class="chat-content">${escapeHtml(m.content || '')}</div>
      ${(m.mentions || []).length ? `<div class="chat-mentions">@${m.mentions.join(', @')}</div>` : ''}
      <div class="chat-meta">${fmtTime(m.ts)} · ${m.type || ''}</div>
    </div>
  </div>`;
}

async function sendChannelMessage() {
  const channel = state.currentChannel;
  if (!channel) { showToast('请先选择频道', 'error'); return; }
  const from = $('ch-msg-from')?.value || 'god';
  const mentions = ($('ch-msg-mentions')?.value || '').split(',').filter(Boolean);
  const type = $('ch-msg-type')?.value || 'mention';
  const content = $('ch-msg-content')?.value;
  if (!content?.trim()) return;

  try {
    await api(`/api/channels/${channel}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from: from.trim(), content: content.trim(), type, mentions }),
    });
    $('ch-msg-content').value = '';
    await refreshChannelDetail();
    showToast('消息已发送');
  } catch (e) {
    showToast('发送失败: ' + e.message, 'error');
  }
}
window.sendChannelMessage = sendChannelMessage;

async function addMemberToCurrentChannel() {
  const ch = state.currentChannel;
  const sel = $('add-member-select');
  if (!ch || !sel?.value) return;
  try {
    await api(`/api/channels/${ch}/config`, {
      method: 'PUT',
      body: JSON.stringify({ add_members: [sel.value] }),
    });
    showToast('成员已添加');
    refreshChannelDetail();
  } catch (e) { showToast('添加失败: ' + e.message, 'error'); }
}
window.addMemberToCurrentChannel = addMemberToCurrentChannel;

async function addAdminToCurrentChannel() {
  const ch = state.currentChannel;
  const sel = $('add-admin-select');
  if (!ch || !sel?.value) return;
  try {
    await api(`/api/channels/${ch}/config`, {
      method: 'PUT',
      body: JSON.stringify({ add_admins: [sel.value] }),
    });
    showToast('管理员已添加');
    refreshChannelDetail();
  } catch (e) { showToast('添加失败: ' + e.message, 'error'); }
}
window.addAdminToCurrentChannel = addAdminToCurrentChannel;

async function removeMemberFromCurrentChannel(memberId) {
  const ch = state.currentChannel;
  if (!ch || !confirm(`移除 ${memberId}?`)) return;
  try {
    await api(`/api/channels/${ch}/config`, {
      method: 'PUT',
      body: JSON.stringify({ remove_members: [memberId] }),
    });
    showToast('成员已移除');
    refreshChannelDetail();
  } catch (e) { showToast('移除失败: ' + e.message, 'error'); }
}
window.removeMemberFromCurrentChannel = removeMemberFromCurrentChannel;

async function clearChannelMessages() {
  const ch = state.currentChannel;
  if (!ch || !confirm(`清空 ${ch} 的所有消息?`)) return;
  try {
    await api(`/api/channels/${ch}/messages`, { method: 'DELETE' });
    showToast('已清空');
    refreshChannelDetail();
  } catch (e) { showToast('清空失败: ' + e.message, 'error'); }
}
window.clearChannelMessages = clearChannelMessages;

// ============================
// 频道设置 modal
// ============================

async function showChannelSettings(name) {
  if (!name) return;
  try {
    const meta = await api(`/api/channels/${name}/meta`);
    $('settings-channel-name').textContent = name;
    $('settings-max-messages').value = meta.max_messages || 0;
    $('settings-enabled-workers').value = (meta.enabled_workers || []).join(', ');
    $('channel-settings-modal').classList.remove('hidden');
  } catch (e) { showToast('加载失败: ' + e.message, 'error'); }
}
window.showChannelSettings = showChannelSettings;

function hideChannelSettings() {
  $('channel-settings-modal').classList.add('hidden');
}
window.hideChannelSettings = hideChannelSettings;

async function saveChannelSettings() {
  const ch = state.currentChannel;
  if (!ch) return;
  const maxMsgs = parseInt($('settings-max-messages')?.value || '0');
  const enabled = ($('settings-enabled-workers')?.value || '').split(',').filter(Boolean);
  try {
    await api(`/api/channels/${ch}/config`, {
      method: 'PUT',
      body: JSON.stringify({ max_messages: maxMsgs, enabled_workers: enabled }),
    });
    showToast('设置已保存');
    hideChannelSettings();
    refreshChannelDetail();
  } catch (e) { showToast('保存失败: ' + e.message, 'error'); }
}
window.saveChannelSettings = saveChannelSettings;

// ============================
// 实时聊天
// ============================

async function refreshLiveChat() {
  const ch = state.liveChatChannel || state.currentChannel;
  state.liveChatChannel = ch;
  $('live-channel-title').textContent = ch ? `· ${ch}` : '';

  const el = $('live-chat-messages');
  if (!el) return;

  if (!ch) {
    el.innerHTML = '<div class="empty-state">选择顶部频道开始观察</div>';
    return;
  }

  try {
    const data = await api(`/api/channels/${ch}/messages?limit=100`);
    const msgs = data.messages || [];

    // 如果有 lastMessageId, 只在有新消息时更新
    if (state.lastMessageId && msgs.length > 0 && msgs[msgs.length - 1].id === state.lastMessageId) {
      return;  // 无新消息
    }

    if (msgs.length > 0) {
      state.lastMessageId = msgs[msgs.length - 1].id;
    }

    el.innerHTML = msgs.length
      ? msgs.map(m => renderMessage(m)).join('')
      : '<div class="empty-state">暂无消息</div>';

    // 自动滚动
    if ($('auto-scroll-check')?.checked) {
      el.scrollTop = el.scrollHeight;
    }
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

function startLiveChatRefresh() {
  if (!state.liveChatChannel && state.currentChannel) {
    state.liveChatChannel = state.currentChannel;
  }
  refreshLiveChat();
  refreshWorkerStatusPanel();  // 首次加载右侧面板
  stopLiveChatRefresh();
  state.liveChatTimer = setInterval(() => {
    refreshLiveChat();
    refreshWorkerStatusPanel();  // 每次轮询都更新状态
  }, 2000);
}

function stopLiveChatRefresh() {
  if (state.liveChatTimer) {
    clearInterval(state.liveChatTimer);
    state.liveChatTimer = null;
  }
  state.lastMessageId = null;  // 重置以便重新进入时刷新
}

// ============================
// 聊天面板右侧: Worker 实时状态
// ============================

async function refreshWorkerStatusPanel() {
  const listEl = $('worker-status-list');
  const metaEl = $('worker-status-meta');
  if (!listEl) return;

  try {
    const ags = await api('/api/agents');
    if (metaEl) metaEl.textContent = `${ags.length} 个`;

    if (ags.length === 0) {
      listEl.innerHTML = '<div class="empty-state">暂无 Worker</div>';
      return;
    }

    // 并发获取每个 worker 的完整状态: PDR + mailbox + active session
    const statuses = await Promise.all(ags.map(async a => {
      const result = { agent: a, pdr: null, mails: 0, last_log_line: '' };
      try {
        const p = await api(`/api/agents/${a.agent_id}/pdr-status`);
        result.pdr = p.pdr || p;
      } catch (e) {}
      try {
        const m = await api(`/api/mailboxes/${a.agent_id}`);
        result.mails = m.count || 0;
      } catch (e) {}
      try {
        const log = await api(`/api/agents/${a.agent_id}/log?tail=10`);
        const lines = (log.log || '').split('\n').filter(Boolean);
        result.last_log_line = lines[lines.length - 1] || '';
      } catch (e) {}
      return result;
    }));

    listEl.innerHTML = statuses.map(s => renderWorkerStatusCard(s)).join('');
  } catch (e) {
    listEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

function deriveStatus(pdr) {
  // 从 PDR 数据推导状态:
  //  - processing: 邮箱有未处理 或 有活跃 session
  //  - waiting: 决策 ready=false 或 等待回应
  //  - idle: 空闲
  if (!pdr) return 'unknown';
  const pending = pdr.perceive?.pending_mails_count || 0;
  const activeSessions = pdr.remember?.active_sessions_count || 0;
  const mode = pdr.decide?.mode || '?';
  const subscriptions = pdr.perceive?.subscriptions || [];

  if (pending > 0 || activeSessions > 0) return 'processing';
  if (mode === 'proactive' && subscriptions.length > 0) return 'waiting';
  return 'idle';
}

function renderWorkerStatusCard(s) {
  const a = s.agent;
  const pdr = s.pdr;
  const status = deriveStatus(pdr);
  const statusText = {
    processing: '处理中',
    waiting: '等待中',
    idle: '空闲',
    unknown: '未知',
  }[status];
  const cliType = pdr?.act?.cli_type || '?';
  const model = pdr?.act?.model || '';
  const mode = pdr?.decide?.mode || '?';
  const subscriptions = pdr?.perceive?.subscriptions || [];
  const pending = pdr?.perceive?.pending_mails_count || 0;
  const lastDecision = pdr?.decide?.last_decision || '';
  const activeSessions = pdr?.remember?.active_sessions || [];
  const lastLog = s.last_log_line || '';

  return `
    <div class="ws-card ${status}" onclick="showWorkerDetail('${escapeHtml(a.agent_id)}')">
      <div class="ws-card-header">
        <span class="ws-name">${escapeHtml(a.agent_id)}</span>
        <span class="ws-status-badge ${status}">● ${statusText}</span>
      </div>
      <div class="ws-card-row">
        <span class="ws-label">⚡ CLI</span>
        <span class="ws-value">${escapeHtml(cliType)}</span>
      </div>
      ${model ? `<div class="ws-card-row">
        <span class="ws-label">🧠 model</span>
        <span class="ws-value ws-value-mono">${escapeHtml(model)}</span>
      </div>` : ''}
      <div class="ws-card-row">
        <span class="ws-label">📡 邮箱</span>
        <span class="ws-value">${pending} 待处理${pending > 0 ? ' 📬' : ''}</span>
      </div>
      <div class="ws-card-row">
        <span class="ws-label">📺 模式</span>
        <span class="ws-value mode-${mode}">${mode}${subscriptions.length ? ' [' + subscriptions.join(',') + ']' : ''}</span>
      </div>
      ${activeSessions.length > 0 ? `
      <div class="ws-section">
        <span class="ws-section-title">💭 活跃 Session (${activeSessions.length})</span>
        ${activeSessions.map(sess => `
          <div class="ws-session">
            <div class="ws-session-id">${escapeHtml(sess.session_id || '?')}</div>
            <div class="ws-session-topic">${escapeHtml(sess.topic || '')}</div>
            <div class="ws-session-progress">
              <div class="progress-bar"><div class="progress-fill" style="width:${sess.progress || 0}%"></div></div>
              <span class="ws-progress-num">${sess.progress || 0}%</span>
            </div>
            ${sess.next_action ? `<div class="ws-session-next">→ ${escapeHtml(sess.next_action)}</div>` : ''}
            ${sess.summary ? `<div class="ws-session-summary">${escapeHtml(sess.summary)}</div>` : ''}
          </div>
        `).join('')}
      </div>
      ` : ''}
      ${lastDecision ? `
      <div class="ws-section">
        <span class="ws-section-title">🎯 最后决策</span>
        <div class="ws-decision">${escapeHtml(lastDecision.slice(-150))}</div>
      </div>
      ` : ''}
      ${lastLog ? `
      <div class="ws-section">
        <span class="ws-section-title">📄 最新日志</span>
        <div class="ws-log">${escapeHtml(lastLog.slice(-200))}</div>
      </div>
      ` : ''}
    </div>
  `;
}

async function sendLiveMessage() {
  const ch = state.liveChatChannel;
  if (!ch) { showToast('请先选择频道', 'error'); return; }
  const from = $('live-from')?.value || 'god';
  const content = $('live-content')?.value;
  if (!content?.trim()) return;

  // 自动从内容中提取 @mention
  const mentions = (content.match(/@(\w[\w-]*)/g) || []).map(m => m.slice(1));

  try {
    await api(`/api/channels/${ch}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from, content: content.trim(), type: 'mention', mentions }),
    });
    $('live-content').value = '';
    setTimeout(refreshLiveChat, 200);
    showToast('已发送');
  } catch (e) { showToast('发送失败: ' + e.message, 'error'); }
}
window.sendLiveMessage = sendLiveMessage;

// Live chat Enter 键发送
$('live-content')?.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendLiveMessage();
  }
});

// ============================
// Workers
// ============================

async function refreshWorkers() {
  const el = $('workers-content');
  if (!el) return;

  try {
    const ags = await api('/api/agents');
    if (ags.length === 0) {
      el.innerHTML = '<div class="empty-state">暂无 Worker<br><button class="btn btn-primary" onclick="showNewWorkerModal()">+ 新建 Worker</button></div>';
      return;
    }

    // 并发获取每个 worker 的 PDR
    const pdrMap = {};
    await Promise.all(ags.map(async a => {
      try {
        const p = await api(`/api/agents/${a.agent_id}/pdr-status`);
        pdrMap[a.agent_id] = p.pdr || p;
      } catch (e) { pdrMap[a.agent_id] = null; }
    }));

    el.innerHTML = `
      <div class="workers-grid">
        ${ags.map(agent => {
          const pdr = pdrMap[agent.agent_id];
          return `
          <div class="worker-card" onclick="showWorkerDetail('${agent.agent_id}')">
            <div class="worker-header">
              <div class="worker-name">${escapeHtml(agent.agent_id)}</div>
              <div class="worker-status ${agent.pending > 0 ? 'has-mail' : 'idle'}">
                ${agent.pending > 0 ? `📬 ${agent.pending}` : '🟢'}
              </div>
            </div>
            ${pdr ? `
            <div class="pdr-mini">
              <div class="pdr-mini-row"><span class="pdr-label">📡 Perceive</span><span>${pdr.perceive?.pending_mails_count || 0} 待处理 · ${(pdr.perceive?.subscriptions || []).length || 0} 订阅</span></div>
              <div class="pdr-mini-row"><span class="pdr-label">🧠 Decide</span><span>${escapeHtml(pdr.decide?.mode || '?')}</span></div>
              <div class="pdr-mini-row"><span class="pdr-label">💾 Remember</span><span>${pdr.remember?.active_sessions_count || 0} 活跃 session</span></div>
              <div class="pdr-mini-row"><span class="pdr-label">⚡ Act</span><span>${escapeHtml(pdr.act?.cli_type || '?')}</span></div>
            </div>
            ` : '<div class="empty-state">PDR 数据加载失败</div>'}
            <div class="worker-actions" onclick="event.stopPropagation()">
              <button class="btn btn-sm" onclick="startWorkerDirect('${agent.agent_id}')">▶ 启动</button>
              <button class="btn btn-sm btn-warn" onclick="stopWorkerDirect('${agent.agent_id}')">⏹ 停止</button>
              <button class="btn btn-sm" onclick="showWorkerDetail('${agent.agent_id}')">🔍 详情</button>
            </div>
          </div>
          `;
        }).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

async function startWorkerDirect(agentId) {
  try {
    await api(`/api/agents/${agentId}/start`, { method: 'POST' });
    showToast(`已启动 ${agentId}`);
    setTimeout(refreshWorkers, 2000);
  } catch (e) { showToast('启动失败: ' + e.message, 'error'); }
}
window.startWorkerDirect = startWorkerDirect;

async function stopWorkerDirect(agentId) {
  try {
    await api(`/api/agents/${agentId}/stop`, { method: 'POST' });
    showToast(`停止信号已发送`);
    setTimeout(refreshWorkers, 2000);
  } catch (e) { showToast('停止失败: ' + e.message, 'error'); }
}
window.stopWorkerDirect = stopWorkerDirect;

// ============================
// Worker 详情 Modal
// ============================

async function showWorkerDetail(agentId) {
  state.activeWorkerDetail = agentId;
  $('worker-detail-name').textContent = agentId;
  $('worker-detail-modal').classList.remove('hidden');

  // 切换 tab 监听
  document.querySelectorAll('#worker-detail-modal .tab').forEach(t => {
    t.onclick = () => {
      document.querySelectorAll('#worker-detail-modal .tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('#worker-detail-modal .tab-pane').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      document.getElementById('tab-' + t.dataset.tab).classList.add('active');
    };
  });

  await loadWorkerTab('pdr');
  loadWorkerTab('config');
  loadWorkerTab('mailbox');
  loadWorkerTab('sessions');
  loadWorkerTab('log');
  loadWorkerTab('workspace');
}
window.showWorkerDetail = showWorkerDetail;

function closeWorkerDetail() {
  $('worker-detail-modal').classList.add('hidden');
  state.activeWorkerDetail = '';
}
window.closeWorkerDetail = closeWorkerDetail;

async function loadWorkerTab(tab) {
  const agentId = state.activeWorkerDetail;
  if (!agentId) return;
  const el = $('tab-' + tab);
  if (!el) return;
  el.innerHTML = '加载中...';
  try {
    if (tab === 'pdr') {
      const r = await api(`/api/agents/${agentId}/pdr-status`);
      const pdr = r.pdr || r;
      el.innerHTML = `
        <div class="pdr-detail">
          <div class="pdr-section">
            <h4>📡 Perceive (感知)</h4>
            <div>邮箱待处理: <strong>${pdr.perceive?.pending_mails_count || 0}</strong> 封</div>
            <div>订阅频道: ${(pdr.perceive?.subscriptions || []).join(', ') || '无'}</div>
          </div>
          <div class="pdr-section">
            <h4>🧠 Decide (决策)</h4>
            <div>运行模式: <strong>${escapeHtml(pdr.decide?.mode || '?')}</strong></div>
            <div>最后决策: <code>${escapeHtml((pdr.decide?.last_decision || '无').slice(0, 100))}</code></div>
          </div>
          <div class="pdr-section">
            <h4>💾 Remember (记忆)</h4>
            <div>活跃 Sessions: <strong>${pdr.remember?.active_sessions_count || 0}</strong></div>
            <div>Session 详情: <pre>${escapeHtml(JSON.stringify(pdr.remember?.active_sessions || [], null, 2))}</pre></div>
          </div>
          <div class="pdr-section">
            <h4>⚡ Act (执行)</h4>
            <div>CLI 类型: <strong>${escapeHtml(pdr.act?.cli_type || '?')}</strong></div>
            <div>Model: <code>${escapeHtml(pdr.act?.model || '?')}</code></div>
            <div>Workspace: <code>${escapeHtml(pdr.act?.workspace_dir || '?')}</code></div>
          </div>
        </div>
      `;
    } else if (tab === 'config') {
      const r = await api(`/api/agents/${agentId}/config`).catch(() => null);
      if (!r) {
        el.innerHTML = '<div class="empty-state">无 config.json</div>';
      } else {
        el.innerHTML = `<pre class="config-json">${escapeHtml(JSON.stringify(r.config, null, 2))}</pre>`;
      }
    } else if (tab === 'mailbox') {
      const r = await api(`/api/mailboxes/${agentId}`);
      const mails = r.mails || [];
      el.innerHTML = mails.length
        ? mails.map(m => `<div class="mail-item">
            <div class="mail-header"><b>${escapeHtml(m.from || '?')}</b> · ${fmtTime(m.ts)}</div>
            <div class="mail-content">${escapeHtml((m.content || '').slice(0, 500))}</div>
          </div>`).join('')
        : '<div class="empty-state">邮箱为空</div>';
    } else if (tab === 'sessions') {
      const [all, active] = await Promise.all([
        api(`/api/sessions/${agentId}`).catch(() => ({ sessions: [] })),
        api(`/api/sessions/${agentId}/active`).catch(() => ({ sessions: [] })),
      ]);
      const allS = all.sessions || [];
      const actS = active.sessions || [];
      el.innerHTML = `
        <h4>活跃 (${actS.length})</h4>
        ${actS.length ? actS.map(s => `<div class="session-mini">
          <div><b>${escapeHtml(s.session_id || '')}</b> · ${escapeHtml(s.topic || '')}</div>
          <div>进度: ${s.progress || 0}%</div>
        </div>`).join('') : '<div class="empty-state">无活跃 session</div>'}
        <h4>全部 (${allS.length})</h4>
        ${allS.length ? allS.map(s => `<div class="session-mini">
          <div><b>${escapeHtml(s.session_id || '')}</b> · ${escapeHtml(s.topic || '')}</div>
          <div>进度: ${s.progress || 0}%</div>
        </div>`).join('') : '<div class="empty-state">无 session</div>'}
      `;
    } else if (tab === 'log') {
      const r = await api(`/api/agents/${agentId}/log?tail=200`);
      el.innerHTML = `<pre class="log-pre">${escapeHtml(r.log || '(无日志)')}</pre>`;
    } else if (tab === 'workspace') {
      const r = await api(`/api/agents/${agentId}/workspace`);
      if (!r.exists) {
        el.innerHTML = '<div class="empty-state">无 workspace 目录</div>';
      } else if (!r.files.length) {
        el.innerHTML = '<div class="empty-state">workspace 为空</div>';
      } else {
        el.innerHTML = r.files.map(f => `<div class="ws-file">
          <span class="ws-path">${escapeHtml(f.path)}</span>
          <span class="ws-size">${f.size}B</span>
        </div>`).join('');
      }
    }
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

function startWorkerFromModal() {
  if (state.activeWorkerDetail) startWorkerDirect(state.activeWorkerDetail);
}
window.startWorkerFromModal = startWorkerFromModal;

function stopWorkerFromModal() {
  if (state.activeWorkerDetail) stopWorkerDirect(state.activeWorkerDetail);
}
window.stopWorkerFromModal = stopWorkerFromModal;

// ============================
// 任务 (Tasks / StateBoard)
// ============================

async function refreshTasks() {
  const el = $('tasks-content');
  if (!el) return;

  // 刷新过滤下拉框
  const filter = $('task-filter-channel');
  if (filter) {
    try {
      const chs = await api('/api/channels');
      const cur = filter.value;
      filter.innerHTML = '<option value="">所有频道</option>' +
        chs.map(c => `<option value="${c.name}" ${cur === c.name ? 'selected' : ''}>${c.name}</option>`).join('');
    } catch (e) {}
  }

  try {
    const r = await api('/api/state_board');
    const tasks = r.tasks || {};
    const list = Object.entries(tasks).map(([tid, t]) => ({ tid, ...t }));
    const channelFilter = filter?.value || '';

    el.innerHTML = list.length
      ? `<div class="tasks-grid">${list.map(t => `
          <div class="task-card">
            <div class="task-id">${escapeHtml(t.tid)}</div>
            <div class="task-progress">
              <div class="progress-bar"><div class="progress-fill" style="width: ${t.progress || 0}%"></div></div>
              <span>${t.progress || 0}%</span>
            </div>
            <div class="task-summary">${escapeHtml(t.summary || '')}</div>
            <div class="task-meta">
              <span>👤 ${escapeHtml(t.agent || '?')}</span>
              <span>📺 ${escapeHtml(t.channel || '?')}</span>
            </div>
            <div class="task-next">下一步: ${escapeHtml(t.next_action || '')}</div>
          </div>
        `).join('')}</div>`
      : '<div class="empty-state">任务板为空</div>';
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

$('task-filter-channel')?.addEventListener('change', refreshTasks);

// ============================
// 邮箱
// ============================

async function refreshMailboxes() {
  const el = $('mailboxes-content');
  if (!el) return;

  try {
    const ags = await api('/api/agents');
    if (ags.length === 0) {
      el.innerHTML = '<div class="empty-state">暂无 Worker<br><button class="btn" onclick="showNewWorkerModal()">+ 新建</button></div>';
      return;
    }

    // 获取每个 worker 的邮箱
    const mbs = await Promise.all(ags.map(async a => {
      try {
        const r = await api(`/api/mailboxes/${a.agent_id}`);
        return { agent: a, mails: r.mails || [], count: r.count || 0 };
      } catch (e) {
        return { agent: a, mails: [], count: 0 };
      }
    }));

    el.innerHTML = `
      <div class="mailbox-grid">
        ${mbs.map(({ agent, mails, count }) => `
          <div class="mailbox-card ${count > 0 ? 'has-mail' : ''}">
            <div class="mailbox-header">
              <span class="mailbox-name" onclick="showWorkerDetail('${agent.agent_id}')">📥 ${escapeHtml(agent.agent_id)}</span>
              <span class="mailbox-count">${count} 封</span>
              <button class="btn btn-xs" onclick="clearMailbox('${agent.agent_id}')">🗑</button>
            </div>
            <div class="mailbox-body">
              ${mails.length === 0
                ? '<div class="empty-state">邮箱为空</div>'
                : mails.slice(0, 5).map(m => `<div class="mail-mini">
                    <div><b>${escapeHtml(m.from || '?')}</b> · ${fmtTime(m.ts)}</div>
                    <div class="mail-content-mini">${escapeHtml((m.content || '').slice(0, 80))}</div>
                  </div>`).join('') + (mails.length > 5 ? `<div class="more">+ ${mails.length - 5} 更多...</div>` : '')}
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function clearMailbox(agentId) {
  if (!confirm(`清空 ${agentId} 邮箱?`)) return;
  try {
    await api(`/api/mailboxes/${agentId}`, { method: 'DELETE' });
    showToast('已清空');
    refreshMailboxes();
  } catch (e) { showToast('清空失败: ' + e.message, 'error'); }
}
window.clearMailbox = clearMailbox;

// ============================
// Modals
// ============================

function showNewChannelModal() {
  $('new-channel-modal').classList.remove('hidden');
  $('new-channel-name').focus();
}
window.showNewChannelModal = showNewChannelModal;
function hideNewChannelModal() { $('new-channel-modal').classList.add('hidden'); }
window.hideNewChannelModal = hideNewChannelModal;

async function createChannel() {
  const name = $('new-channel-name')?.value?.trim();
  const maxMsgs = parseInt($('new-channel-max')?.value || '0');
  const members = ($('new-channel-members')?.value || '').split(',').filter(Boolean);
  const admins = ($('new-channel-admins')?.value || '').split(',').filter(Boolean);
  if (!name) { showToast('请填写频道名', 'error'); return; }

  try {
    // 1. 创建频道 (post 一条消息)
    await api(`/api/channels/${name}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from: 'god', content: 'init channel', type: 'text' }),
    });
    // 2. 设置 max_messages + members + admins
    await api(`/api/channels/${name}/config`, {
      method: 'PUT',
      body: JSON.stringify({
        max_messages: maxMsgs,
        add_members: members,
        add_admins: admins,
      }),
    });
    $('new-channel-name').value = '';
    $('new-channel-members').value = '';
    $('new-channel-admins').value = '';
    hideNewChannelModal();
    showToast(`频道 ${name} 已创建`);
    // 自动切到新频道
    state.currentChannel = name;
    $('current-channel-select').value = name;
    refresh();
  } catch (e) { showToast('创建失败: ' + e.message, 'error'); }
}
window.createChannel = createChannel;

function showNewWorkerModal() {
  $('new-worker-modal').classList.remove('hidden');
  $('new-worker-id').focus();
}
window.showNewWorkerModal = showNewWorkerModal;
function hideNewWorkerModal() { $('new-worker-modal').classList.add('hidden'); }
window.hideNewWorkerModal = hideNewWorkerModal;

async function createWorker() {
  const id = $('new-worker-id')?.value?.trim();
  const cli = $('new-worker-cli')?.value || 'opencode';
  const mode = $('new-worker-mode')?.value || 'passive';
  const subs = ($('new-worker-subs')?.value || '').split(',').filter(Boolean);
  const role = $('new-worker-role')?.value?.trim();
  const prompt = $('new-worker-prompt')?.value?.trim();

  if (!id) { showToast('请填写 Worker ID', 'error'); return; }

  try {
    // 1. 启动 worker (会创建 config.json + role.md)
    await api(`/api/agents/${id}/start`, { method: 'POST' });
    showToast(`Worker ${id} 创建中...`);
    $('new-worker-id').value = '';
    hideNewWorkerModal();
    setTimeout(refresh, 2000);
  } catch (e) { showToast('创建失败: ' + e.message, 'error'); }
}
window.createWorker = createWorker;

async function doReset() {
  if (!confirm('重置系统? 这会清空所有 sessions 和 mailboxes (频道保留).')) return;
  try {
    await api('/api/reset', { method: 'POST' });
    showToast('已重置');
    refresh();
  } catch (e) { showToast('重置失败: ' + e.message, 'error'); }
}
window.doReset = doReset;

// ============================
// 启动
// ============================

(async () => {
  await refresh();
  startAutoRefresh();
})();