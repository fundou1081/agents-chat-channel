// agents-chat-channel v2.0 · WebUI JavaScript

const API = '';
let state = {
  currentView: 'dashboard',
  currentChannel: null,
  refreshTimer: null,
  refreshInterval: 5000,
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
  refresh();
}

// ============================
// 自动刷新
// ============================

$('auto-refresh')?.addEventListener('change', e => {
  if (e.target.checked) startAutoRefresh();
  else stopAutoRefresh();
});

$('refresh-btn')?.addEventListener('click', refresh);
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

  switch (state.currentView) {
    case 'dashboard': await refreshDashboard(); break;
    case 'channels': await refreshChannels(); break;
    case 'workers': await refreshWorkers(); break;
    case 'mailboxes': await refreshMailboxes(); break;
    case 'sessions': await refreshSessions(); break;
    case 'state-board': await refreshStateBoard(); break;
    case 'stats': await refreshStats(); break;
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
    const bd = await $('badge-channels');
    const bw = $('badge-workers');
    if (bd) bd.textContent = chs.length || '';
    if (bw) bw.textContent = ags.length || '';
  } catch {}
}

// ============================
// Dashboard
// ============================

async function refreshDashboard() {
  const el = $('dashboard-content');
  if (!el) return;

  try {
    const [chs, ags, bd] = await Promise.all([
      api('/api/channels'),
      api('/api/agents'),
      api('/api/state_board'),
    ]);

    const tasks = bd.tasks || {};
    const taskCount = Object.keys(tasks).length;

    el.innerHTML = `
      <div class="card-grid">
        <div class="card" onclick="switchView('channels')">
          <div class="card-icon">💬</div>
          <div class="card-label">频道</div>
          <div class="card-value">${chs.length}</div>
          <div class="card-sub">${chs.map(c => c.name).join(', ') || '无'}</div>
        </div>
        <div class="card" onclick="switchView('workers')">
          <div class="card-icon">🤖</div>
          <div class="card-label">Workers</div>
          <div class="card-value">${ags.length}</div>
          <div class="card-sub">${ags.map(a => a.agent_id).join(', ') || '无'}</div>
        </div>
        <div class="card" onclick="switchView('state-board')">
          <div class="card-icon">📋</div>
          <div class="card-label">任务</div>
          <div class="card-value">${taskCount}</div>
          <div class="card-sub">${Object.keys(tasks).slice(0,3).join(', ') || '无'}</div>
        </div>
        <div class="card">
          <div class="card-icon">📥</div>
          <div class="card-label">待处理邮件</div>
          <div class="card-value">${ags.reduce((s, a) => s + (a.pending || 0), 0)}</div>
          <div class="card-sub">所有 worker 合计</div>
        </div>
      </div>

      <div class="section">
        <h3>最近消息 (所有频道)</h3>
        <div class="recent-messages">
          ${chs.slice(0, 5).map(ch => {
            const msgs = ch.messages || [];
            const last = msgs[msgs.length - 1];
            if (!last) return '';
            return `<div class="msg-item" onclick="selectChannel('${ch.name}')">
              <span class="msg-channel">${ch.name}</span>
              <span class="msg-from">${escapeHtml(last.from || '')}</span>
              <span class="msg-content">${escapeHtml((last.content || '').slice(0, 60))}</span>
              <span class="msg-time">${fmtRelTime(last.ts)}</span>
            </div>`;
          }).join('')}
          ${chs.length === 0 ? '<div class="empty-state">暂无频道</div>' : ''}
        </div>
      </div>

      <div class="section">
        <h3>快捷操作</h3>
        <div class="quick-actions">
          <button class="btn btn-primary" onclick="showSendModal()">✉️ 发送消息</button>
          <button class="btn" onclick="showNewChannelModal()">+ 新建频道</button>
          <button class="btn" onclick="showNewWorkerModal()">+ 新建 Worker</button>
          <button class="btn btn-warn" onclick="doReset()">🗑️ 重置系统</button>
        </div>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// ============================
// Channels
// ============================

async function refreshChannels() {
  const listEl = $('channel-list');
  const detailEl = $('channel-detail');
  if (!listEl) return;

  try {
    const chs = await api('/api/channels');
    listEl.innerHTML = chs.map(ch => `
      <div class="channel-item ${state.currentChannel === ch.name ? 'active' : ''}"
           onclick="selectChannel('${ch.name}')">
        <div class="channel-name">${escapeHtml(ch.name)}</div>
        <div class="channel-meta">
          ${ch.member_count != null ? `<span>${ch.member_count} 成员</span>` : ''}
          ${ch.max_messages ? `<span>限${ch.max_messages}条</span>` : ''}
          ${ch.enabled_workers?.length ? `<span>白名单</span>` : ''}
        </div>
        <div class="channel-last">${fmtRelTime(ch.last_msg_ts)}</div>
      </div>
    `).join('');

    if (!state.currentChannel || !chs.find(c => c.name === state.currentChannel)) {
      state.currentChannel = chs[0]?.name || null;
    }

    if (state.currentChannel) {
      await loadChannelDetail(state.currentChannel);
    } else if (detailEl) {
      detailEl.innerHTML = '<div class="empty-state">选择频道查看详情</div>';
    }
  } catch (e) {
    listEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function selectChannel(name) {
  state.currentChannel = name;
  await refreshChannels();
}

async function loadChannelDetail(name) {
  const detailEl = $('channel-detail');
  if (!detailEl) return;

  try {
    const [meta, msgs] = await Promise.all([
      api(`/api/channels/${name}/meta`),
      api(`/api/channels/${name}/messages?limit=50`),
    ]);

    const admins = meta.admins || [];
    const members = meta.members || [];
    const enabledWorkers = meta.enabled_workers || [];

    detailEl.innerHTML = `
      <div class="channel-header">
        <h3>${escapeHtml(name)}</h3>
        <div class="channel-actions">
          <button class="btn btn-sm" onclick="showChannelMetaModal('${name}')">⚙️ 设置</button>
          <button class="btn btn-sm btn-warn" onclick="clearChannel('${name}')">🗑 清空</button>
        </div>
      </div>

      <div class="channel-meta-info">
        <div class="meta-row">
          <span class="meta-label">admin:</span>
          <span>${admins.map(a => escapeHtml(a)).join(', ') || '无'}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">members:</span>
          <span>${members.map(m => escapeHtml(m)).join(', ') || '无'}</span>
        </div>
        ${enabledWorkers.length ? `
        <div class="meta-row">
          <span class="meta-label">白名单:</span>
          <span>${enabledWorkers.map(w => escapeHtml(w)).join(', ')}</span>
        </div>` : ''}
        ${meta.max_messages ? `
        <div class="meta-row">
          <span class="meta-label">max_messages:</span>
          <span>${meta.max_messages}</span>
        </div>` : ''}
      </div>

      <div class="channel-members">
        <h4>成员管理</h4>
        <div class="member-row">
          <input type="text" id="add-member-input" placeholder="agent_id">
          <button class="btn btn-sm" onclick="addMember('${name}')">+ 成员</button>
        </div>
        <div class="member-row">
          <input type="text" id="add-admin-input" placeholder="admin_id">
          <label><input type="checkbox" id="add-admin-human"> 人类</label>
          <button class="btn btn-sm" onclick="addAdmin('${name}')">+ 管理员</button>
        </div>
      </div>

      <div class="channel-messages">
        <h4>最近消息 <span class="msg-count">${msgs.length}</span></h4>
        <div class="messages-list">
          ${msgs.map(m => `
            <div class="msg-row ${m.from === 'god' ? 'msg-god' : ''}">
              <span class="msg-ts">${fmtTime(m.ts)}</span>
              <span class="msg-from">${escapeHtml(m.from || '')}</span>
              ${(m.mentions || []).length ? `<span class="msg-mentions">@${m.mentions.join(',@')}</span>` : ''}
              <span class="msg-type">${m.type || ''}</span>
              <span class="msg-content">${escapeHtml((m.content || '').slice(0, 120))}</span>
            </div>
          `).join('')}
          ${msgs.length === 0 ? '<div class="empty-state">暂无消息</div>' : ''}
        </div>
      </div>

      <div class="channel-send">
        <h4>发消息</h4>
        <div class="send-row">
          <input type="text" id="channel-msg-from" value="god" placeholder="from">
          <input type="text" id="channel-msg-mentions" placeholder="@mention (逗号分隔)">
        </div>
        <div class="send-row">
          <select id="channel-msg-type">
            <option value="mention">mention</option>
            <option value="task_broadcast">task_broadcast</option>
            <option value="text">text</option>
          </select>
        </div>
        <div class="send-row">
          <textarea id="channel-msg-content" rows="3" placeholder="消息内容..."></textarea>
        </div>
        <button class="btn btn-primary" onclick="sendChannelMsg('${name}')">发送</button>
      </div>
    `;
  } catch (e) {
    detailEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function sendChannelMsg(name) {
  const from = $('channel-msg-from')?.value || 'god';
  const mentions = ($('channel-msg-mentions')?.value || '').split(',').filter(Boolean);
  const type = $('channel-msg-type')?.value || 'mention';
  const content = $('channel-msg-content')?.value;
  if (!content?.trim()) return;

  try {
    await api(`/api/channels/${name}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from: from.trim(), content: content.trim(), type, mentions }),
    });
    $('channel-msg-content').value = '';
    await loadChannelDetail(name);
    showToast('消息已发送');
  } catch (e) {
    showToast('发送失败: ' + e.message, 'error');
  }
}

async function addMember(name) {
  const input = $('add-member-input');
  if (!input?.value.trim()) return;
  try {
    await api(`/api/channels/${name}/members`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: input.value.trim() }),
    });
    input.value = '';
    await loadChannelDetail(name);
    showToast('成员已添加');
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

async function addAdmin(name) {
  const input = $('add-admin-input');
  if (!input?.value.trim()) return;
  const isHuman = $('add-admin-human')?.checked || false;
  try {
    await api(`/api/channels/${name}/admins`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: input.value.trim(), is_human: isHuman }),
    });
    input.value = '';
    await loadChannelDetail(name);
    showToast('管理员已添加');
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

async function clearChannel(name) {
  if (!confirm(`确认清空频道 ${name} 的所有消息?`)) return;
  try {
    await api(`/api/channels/${name}/messages`, { method: 'DELETE' });
    await loadChannelDetail(name);
    showToast('已清空');
  } catch (e) {
    showToast('清空失败: ' + e.message, 'error');
  }
}

// ============================
// Workers
// ============================

async function refreshWorkers() {
  const el = $('workers-list');
  if (!el) return;

  try {
    const ags = await api('/api/agents');
    if (ags.length === 0) {
      el.innerHTML = '<div class="empty-state">暂无 Worker<br><button class="btn btn-primary" onclick="showNewWorkerModal()">+ 新建 Worker</button></div>';
      return;
    }

    el.innerHTML = `
      <div class="workers-grid">
        ${ags.map(agent => `
          <div class="worker-card">
            <div class="worker-header">
              <div class="worker-name">${escapeHtml(agent.agent_id)}</div>
              <div class="worker-status ${agent.pending > 0 ? 'has-mail' : 'idle'}">
                ${agent.pending > 0 ? `📬 ${agent.pending} 待处理` : '🟢 空闲'}
              </div>
            </div>
            <div class="worker-info">
              ${agent.log_path ? `<div class="info-row"><span>log:</span><span class="path">${agent.log_path}</span></div>` : ''}
            </div>
            <div class="worker-actions">
              <button class="btn btn-sm" onclick="startWorker('${agent.agent_id}')">▶ 启动</button>
              <button class="btn btn-sm btn-warn" onclick="stopWorker('${agent.agent_id}')">■ 停止</button>
              <button class="btn btn-sm" onclick="viewWorkerLog('${agent.agent_id}')">📄 日志</button>
              <button class="btn btn-sm" onclick="clearWorkerMailbox('${agent.agent_id}')">📥 清邮箱</button>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function startWorker(agentId) {
  try {
    await api(`/api/agents/${agentId}/start`, { method: 'POST' });
    showToast(`启动 ${agentId} ...`);
    setTimeout(refresh, 2000);
  } catch (e) {
    showToast('启动失败: ' + e.message, 'error');
  }
}

async function stopWorker(agentId) {
  try {
    await api(`/api/agents/${agentId}/stop`, { method: 'POST' });
    showToast(`停止信号已发送`);
    setTimeout(refresh, 2000);
  } catch (e) {
    showToast('停止失败: ' + e.message, 'error');
  }
}

async function viewWorkerLog(agentId) {
  try {
    const log = await api(`/api/agents/${agentId}/log?tail=100`);
    const content = log.log || log.error || '无日志';
    alert(`=== ${agentId} 日志 (最后100行) ===\n\n' + content.slice(-3000) + '`);
  } catch (e) {
    showToast('读取日志失败: ' + e.message, 'error');
  }
}

async function clearWorkerMailbox(agentId) {
  if (!confirm(`清空 ${agentId} 的邮箱?`)) return;
  try {
    await api(`/api/mailboxes/${agentId}`, { method: 'DELETE' });
    showToast('邮箱已清空');
    refresh();
  } catch (e) {
    showToast('清空失败: ' + e.message, 'error');
  }
}

// ============================
// Mailboxes
// ============================

async function refreshMailboxes() {
  const listEl = $('mailbox-list');
  const detailEl = $('mailbox-detail');
  if (!listEl) return;

  try {
    const ags = await api('/api/agents');
    const selected = state.selectedMailbox || ags[0]?.agent_id;

    listEl.innerHTML = ags.map(a => `
      <div class="mailbox-item ${selected === a.agent_id ? 'active' : ''}"
           onclick="selectMailbox('${a.agent_id}')">
        <div class="mailbox-name">${escapeHtml(a.agent_id)}</div>
        <div class="mailbox-count">${a.pending || 0} 封</div>
      </div>
    `).join('');

    if (!selected) {
      detailEl.innerHTML = '<div class="empty-state">选择 worker</div>';
      return;
    }

    state.selectedMailbox = selected;
    await loadMailboxDetail(selected);
  } catch (e) {
    listEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function selectMailbox(id) {
  state.selectedMailbox = id;
  await refreshMailboxes();
}

async function loadMailboxDetail(agentId) {
  const detailEl = $('mailbox-detail');
  if (!detailEl) return;

  try {
    const data = await api(`/api/mailboxes/${agentId}`);
    const mails = data.mails || [];
    detailEl.innerHTML = `
      <h3>${escapeHtml(agentId)} 邮箱 <span class="mail-count">${mails.length} 封</span></h3>
      <div class="mails-list">
        ${mails.map((m, i) => `
          <div class="mail-item">
            <div class="mail-header">
              <span class="mail-from">from: ${escapeHtml(m.from || '?')}</span>
              <span class="mail-channel">${escapeHtml(m.channel || '')}</span>
              <span class="mail-time">${fmtTime(m.ts)}</span>
            </div>
            <div class="mail-content">${escapeHtml((m.content || '').slice(0, 200))}</div>
          </div>
        `).join('')}
        ${mails.length === 0 ? '<div class="empty-state">邮箱为空</div>' : ''}
      </div>
    `;
  } catch (e) {
    detailEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

// ============================
// Sessions
// ============================

async function refreshSessions() {
  const listEl = $('session-list');
  const detailEl = $('session-detail');
  if (!listEl) return;

  try {
    const ags = await api('/api/agents');
    const selected = state.selectedSession || ags[0]?.agent_id;

    listEl.innerHTML = ags.map(a => `
      <div class="session-item ${selected === a.agent_id ? 'active' : ''}"
           onclick="selectSession('${a.agent_id}')">
        <div class="session-name">${escapeHtml(a.agent_id)}</div>
      </div>
    `).join('');

    if (!selected) {
      detailEl.innerHTML = '<div class="empty-state">选择 worker</div>';
      return;
    }

    state.selectedSession = selected;
    await loadSessionDetail(selected);
  } catch (e) {
    listEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function selectSession(id) {
  state.selectedSession = id;
  await refreshSessions();
}

async function loadSessionDetail(agentId) {
  const detailEl = $('session-detail');
  if (!detailEl) return;

  try {
    const [allSess, activeSess] = await Promise.all([
      api(`/api/sessions/${agentId}`),
      api(`/api/sessions/${agentId}/active`),
    ]);

    const sessions = allSess.sessions || [];
    const active = activeSess.sessions || [];

    detailEl.innerHTML = `
      <h3>${escapeHtml(agentId)} Sessions <span class="sess-active">${active.length} active</span></h3>

      <h4>活跃 Sessions (${active.length})</h4>
      <div class="sessions-list">
        ${active.map(s => `
          <div class="session-card active">
            <div class="session-id">${escapeHtml(s.session_id || '')}</div>
            <div class="session-topic">${escapeHtml(s.topic || '')}</div>
            <div class="session-meta">
              <span class="progress">${s.progress || 0}%</span>
              <span class="next-action">${escapeHtml(s.next_action || '')}</span>
              <span class="confidence">${escapeHtml(s.confidence || '')}</span>
            </div>
          </div>
        `).join('')}
        ${active.length === 0 ? '<div class="empty-state">无活跃 session</div>' : ''}
      </div>

      <h4>全部 Sessions (${sessions.length})</h4>
      <div class="sessions-list">
        ${sessions.map(s => `
          <div class="session-card">
            <div class="session-id">${escapeHtml(s.session_id || '')}</div>
            <div class="session-topic">${escapeHtml(s.topic || '')}</div>
            <div class="session-meta">
              <span class="progress">${s.progress || 0}%</span>
              <span class="next-action">${escapeHtml(s.next_action || '')}</span>
            </div>
          </div>
        `).join('')}
        ${sessions.length === 0 ? '<div class="empty-state">无 session</div>' : ''}
      </div>
    `;
  } catch (e) {
    detailEl.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

// ============================
// State Board
// ============================

async function refreshStateBoard() {
  const el = $('state-board-list');
  if (!el) return;

  try {
    const bd = await api('/api/state_board');
    const tasks = bd.tasks || {};

    if (Object.keys(tasks).length === 0) {
      el.innerHTML = '<div class="empty-state">任务板为空</div>';
      return;
    }

    el.innerHTML = `
      <div class="tasks-grid">
        ${Object.entries(tasks).map(([taskId, task]) => `
          <div class="task-card">
            <div class="task-id">${escapeHtml(taskId)}</div>
            <div class="task-status">${escapeHtml(task.status || '')}</div>
            <div class="task-progress">
              <div class="progress-bar">
                <div class="progress-fill" style="width: ${task.progress || 0}%"></div>
              </div>
              <span>${task.progress || 0}%</span>
            </div>
            <div class="task-summary">${escapeHtml(task.summary || '')}</div>
            <div class="task-next">下一步: ${escapeHtml(task.next_action || '')}</div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

// ============================
// Stats
// ============================

async function refreshStats() {
  const el = $('stats-content');
  if (!el) return;

  try {
    const [chs, ags, bd] = await Promise.all([
      api('/api/channels'),
      api('/api/agents'),
      api('/api/state_board'),
    ]);

    const tasks = bd.tasks || {};
    const totalSessions = await Promise.allSettled(
      ags.map(a => api(`/api/sessions/${a.agent_id}`).then(r => (r.sessions || []).length))
    ).then(rs => rs.filter(r => r.status === 'fulfilled').reduce((s, r) => s + r.value, 0));

    el.innerHTML = `
      <div class="card-grid">
        <div class="card">
          <div class="card-label">总频道</div>
          <div class="card-value">${chs.length}</div>
        </div>
        <div class="card">
          <div class="card-label">总 Workers</div>
          <div class="card-value">${ags.length}</div>
        </div>
        <div class="card">
          <div class="card-label">总 Sessions</div>
          <div class="card-value">${totalSessions}</div>
        </div>
        <div class="card">
          <div class="card-label">总任务</div>
          <div class="card-value">${Object.keys(tasks).length}</div>
        </div>
      </div>

      <div class="section">
        <h3>频道详情</h3>
        <table class="data-table">
          <thead><tr><th>频道</th><th>成员</th><th>admin</th><th>max_msgs</th><th>白名单</th></tr></thead>
          <tbody>
            ${chs.map(ch => `
              <tr onclick="selectChannel('${ch.name}'); switchView('channels')">
                <td>${escapeHtml(ch.name)}</td>
                <td>${(ch.members || []).length}</td>
                <td>${(ch.admins || []).length}</td>
                <td>${ch.max_messages || '∞'}</td>
                <td>${(ch.enabled_workers || []).join(', ') || '-'}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>

      <div class="section">
        <h3>Worker 状态</h3>
        <table class="data-table">
          <thead><tr><th>Worker</th><th>待处理邮件</th><th>日志路径</th></tr></thead>
          <tbody>
            ${ags.map(a => `
              <tr>
                <td>${escapeHtml(a.agent_id)}</td>
                <td>${a.pending || 0}</td>
                <td class="path">${a.log_path || '-'}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

// ============================
// Modals
// ============================

function showSendModal() { $('send-modal')?.classList.remove('hidden'); refreshChannelsIntoSelect(); }
function hideSendModal() { $('send-modal')?.classList.add('hidden'); }
window.hideSendModal = hideSendModal;

async function refreshChannelsIntoSelect() {
  const sel = $('send-channel');
  if (!sel) return;
  try {
    const chs = await api('/api/channels');
    sel.innerHTML = chs.map(c => `<option value="${c.name}">${c.name}</option>`).join('');
  } catch {}
}

async function sendMessage() {
  const channel = $('send-channel')?.value;
  const from = $('send-from')?.value || 'god';
  const type = $('send-type')?.value || 'mention';
  const mentions = ($('send-mentions')?.value || '').split(',').filter(Boolean);
  const content = $('send-content')?.value;
  if (!channel || !content?.trim()) { showToast('请填写频道和内容', 'error'); return; }
  try {
    await api(`/api/channels/${channel}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from, content: content.trim(), type, mentions }),
    });
    $('send-content').value = '';
    hideSendModal();
    showToast('消息已发送');
    refresh();
  } catch (e) {
    showToast('发送失败: ' + e.message, 'error');
  }
}
window.sendMessage = sendMessage;

function showNewChannelModal() { $('new-channel-modal')?.classList.remove('hidden'); }
function hideNewChannelModal() { $('new-channel-modal')?.classList.add('hidden'); }
window.hideNewChannelModal = hideNewChannelModal;

async function createChannel() {
  const name = $('new-channel-name')?.value?.trim();
  const maxMsgs = parseInt($('new-channel-max')?.value || '0');
  if (!name) { showToast('请填写频道名', 'error'); return; }
  try {
    // 频道通过 post一条消息来自动创建
    await api(`/api/channels/${name}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from: 'god', content: 'init channel', type: 'text' }),
    });
    if (maxMsgs > 0) {
      // 设置 max_messages (通过 meta)
    }
    $('new-channel-name').value = '';
    hideNewChannelModal();
    showToast(`频道 ${name} 已创建`);
    refresh();
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  }
}
window.createChannel = createChannel;

function showNewWorkerModal() { $('new-worker-modal')?.classList.remove('hidden'); }
function hideNewWorkerModal() { $('new-worker-modal')?.classList.add('hidden'); }
window.hideNewWorkerModal = hideNewWorkerModal;

async function createWorker() {
  const id = $('new-worker-id')?.value?.trim();
  if (!id) { showToast('请填写 Worker ID', 'error'); return; }
  try {
    // 创建 mailbox (通过 start agent)
    await api(`/api/agents/${id}/start`, { method: 'POST' });
    $('new-worker-id').value = '';
    hideNewWorkerModal();
    showToast(`Worker ${id} 启动中...`);
    setTimeout(refresh, 2000);
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  }
}
window.createWorker = createWorker;

async function doReset() {
  if (!confirm('确认重置系统? 这将清空所有 sessions 和 mailboxes.')) return;
  try {
    await api('/api/reset', { method: 'POST' });
    showToast('系统已重置');
    refresh();
  } catch (e) {
    showToast('重置失败: ' + e.message, 'error');
  }
}

// ============================
// 启动
// ============================

(async () => {
  await refresh();
  startAutoRefresh();
})();