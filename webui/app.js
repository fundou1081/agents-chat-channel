// agents-chat-channel v2.0 · WebUI JavaScript

const API = '';
let state = {
  currentView: 'channels',  // 默认显示频道管理
  currentChannel: null,
  refreshTimer: null,
  refreshInterval: 5000,
  liveChatChannel: null,
  liveChatTimer: null,
  lastMessageId: null,
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
    case 'live-chat': await refreshLiveChat(); break;
    case 'channels': 
      // 新的频道管理视图
      if ($('channel-list-panel')) {
        await refreshChannelList();
      } else {
        // 兼容旧视图
        await refreshChannels(); 
      }
      break;
    case 'workers': await refreshWorkers(); break;
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
          ${ch.members?.length ? `<span>${ch.members.length} 成员</span>` : ''}
          ${ch.max_messages > 0 ? `<span>限${ch.max_messages}条</span>` : ''}
        </div>
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
  // also expose refresh function
  window.refreshChannelDetail = async (n) => { await loadChannelDetail(n || name); };
  const detailEl = $('channel-detail');
  // Enter key in textarea → send (Shift+Enter =换行)
  setTimeout(() => {
    const ta = $('channel-msg-content');
    if (ta) {
      ta.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendChannelMsg(name);
        }
      });
    }
  }, 100);
  if (!detailEl) return;

  try {
    const [meta, msgsResponse] = await Promise.all([
      api(`/api/channels/${name}/meta`),
      api(`/api/channels/${name}/messages?limit=50`),
    ]);

    const msgs = msgsResponse.messages || [];
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

      <div class="channel-chat-window" id="channel-chat-${name}">
        <div class="chat-messages" id="chat-messages-${name}">
          ${msgs.map(m => {
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
          }).join('')}
          ${msgs.length === 0 ? '<div class="empty-state">暂无消息, 发送一条开始聊天</div>' : ''}
        </div>
      </div>

      <div class="channel-send">
        <div class="send-row">
          <input type="text" id="channel-msg-from" value="god" placeholder="发送者">
          <input type="text" id="channel-msg-mentions" placeholder="@提及 (逗号分隔)">
        </div>
        <div class="send-row">
          <select id="channel-msg-type">
            <option value="mention">mention</option>
            <option value="task_broadcast">task_broadcast</option>
            <option value="text">text</option>
          </select>
        </div>
        <div class="send-row">
          <textarea id="channel-msg-content" rows="2" placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"></textarea>
        </div>
        <div class="send-actions">
          <button class="btn btn-primary" onclick="sendChannelMsg('${name}')">发送</button>
          <button class="btn" onclick="refreshChannelDetail('${name}')">↻ 刷新</button>
        </div>
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

    // 获取每个 worker 的 PDR 状态和配置
    const pdrStatuses = {};
    const configs = {};
    for (const agent of ags) {
      try {
        const pdr = await api(`/api/agents/${agent.agent_id}/pdr-status`);
        pdrStatuses[agent.agent_id] = pdr.pdr;
      } catch (e) {
        console.error(`获取 ${agent.agent_id} PDR 状态失败:`, e);
        pdrStatuses[agent.agent_id] = null;
      }
      
      // 尝试加载 config.json
      try {
        const configResp = await api(`/api/agents/${agent.agent_id}/config`);
        configs[agent.agent_id] = configResp.config;
      } catch (e) {
        // config.json 可能不存在，静默忽略
        configs[agent.agent_id] = null;
      }
    }

    el.innerHTML = `
      <div class="workers-grid">
        ${ags.map(agent => {
          const pdr = pdrStatuses[agent.agent_id];
          return `
          <div class="worker-card">
            <div class="worker-header">
              <div class="worker-name">
                ${escapeHtml(agent.agent_id)}
                ${pdr ? `<span class="cli-badge cli-${pdr.act.cli_type}">${escapeHtml(pdr.act.cli_type)}</span>` : ''}
              </div>
              <div class="worker-status ${agent.pending > 0 ? 'has-mail' : 'idle'}">
                ${agent.pending > 0 ? `📬 ${agent.pending} 待处理` : '🟢 空闲'}
              </div>
            </div>
            
            ${pdr ? `
            <div class="pdr-components">
              <!-- Perceive -->
              <div class="pdr-component perceive">
                <div class="pdr-header">
                  <span class="pdr-icon">📡</span>
                  <span class="pdr-title">Perceive (感知)</span>
                </div>
                <div class="pdr-content">
                  <div class="pdr-item">
                    <span class="pdr-label">邮箱待处理:</span>
                    <span class="pdr-value">${pdr.perceive.pending_mails_count} 封</span>
                  </div>
                  <div class="pdr-item">
                    <span class="pdr-label">订阅频道:</span>
                    <span class="pdr-value">${pdr.perceive.subscriptions.length > 0 ? pdr.perceive.subscriptions.join(', ') : '无'}</span>
                  </div>
                </div>
              </div>
              
              <!-- Decide -->
              <div class="pdr-component decide">
                <div class="pdr-header">
                  <span class="pdr-icon">🧠</span>
                  <span class="pdr-title">Decide (决策)</span>
                </div>
                <div class="pdr-content">
                  <div class="pdr-item">
                    <span class="pdr-label">运行模式:</span>
                    <span class="pdr-value mode-${pdr.decide.mode}">${pdr.decide.mode}</span>
                  </div>
                  <div class="pdr-item">
                    <span class="pdr-label">最后决策:</span>
                    <span class="pdr-value">${pdr.decide.last_decision ? escapeHtml(pdr.decide.last_decision.slice(-50)) : '无'}</span>
                  </div>
                </div>
              </div>
              
              <!-- Remember -->
              <div class="pdr-component remember">
                <div class="pdr-header">
                  <span class="pdr-icon">💾</span>
                  <span class="pdr-title">Remember (记忆)</span>
                </div>
                <div class="pdr-content">
                  <div class="pdr-item">
                    <span class="pdr-label">活跃 Sessions:</span>
                    <span class="pdr-value">${pdr.remember.active_sessions_count}</span>
                  </div>
                  ${pdr.remember.active_sessions.slice(0, 2).map(s => `
                    <div class="session-mini">
                      <span class="session-topic">${escapeHtml(s.topic || '未命名')}</span>
                      <span class="session-progress">${s.progress || 0}%</span>
                    </div>
                  `).join('')}
                </div>
              </div>
              
              <!-- Act -->
              <div class="pdr-component act">
                <div class="pdr-header">
                  <span class="pdr-icon">⚡</span>
                  <span class="pdr-title">Act (执行)</span>
                </div>
                <div class="pdr-content">
                  <div class="pdr-item">
                    <span class="pdr-label">CLI 类型:</span>
                    <span class="pdr-value">${escapeHtml(pdr.act.cli_type)}</span>
                  </div>
                  <div class="pdr-item">
                    <span class="pdr-label">Model:</span>
                    <span class="pdr-value">${escapeHtml(pdr.act.model)}</span>
                  </div>
                </div>
              </div>
            </div>
            ` : ''}
            
            ${configs[agent.agent_id] ? `
            <div class="worker-config">
              <div class="config-header">
                <span class="config-icon">📄</span>
                <span class="config-title">config.json</span>
              </div>
              <div class="config-content">
                <div class="config-item">
                  <span class="config-label">Name:</span>
                  <span class="config-value">${escapeHtml(configs[agent.agent_id].role || agent.agent_id)}</span>
                </div>
                <div class="config-item">
                  <span class="config-label">Workspace:</span>
                  <span class="config-value config-path" title="${escapeHtml(configs[agent.agent_id].workspace || '')}">${escapeHtml((configs[agent.agent_id].workspace || '').split('/').slice(-2).join('/') || 'N/A')}</span>
                </div>
                <div class="config-item">
                  <span class="config-label">Skills:</span>
                  <span class="config-value">${(configs[agent.agent_id].skills || []).length > 0 ? configs[agent.agent_id].skills.join(', ') : '无'}</span>
                </div>
              </div>
            </div>
            ` : ''}
            
            <div class="worker-actions">
              <button class="btn btn-sm" onclick="startWorker('${agent.agent_id}')">▶ 启动</button>
              <button class="btn btn-sm btn-warn" onclick="stopWorker('${agent.agent_id}')">■ 停止</button>
              <button class="btn btn-sm" onclick="viewWorkerLog('${agent.agent_id}')">📄 日志</button>
              <button class="btn btn-sm" onclick="clearWorkerMailbox('${agent.agent_id}')">📥 清邮箱</button>
            </div>
          </div>
        `}).join('')}
      </div>
    `;
  } catch (e) {
    el.innerHTML = `<div class="error">${escapeHtml(e.message)}</div>`;
  }
}

async function loadWorkspaceFiles(agentId) {
  const el = $(`ws-files-${agentId}`);
  if (!el) return;
  try {
    const ws = await api(`/api/agents/${agentId}/workspace`);
    if (!ws.exists) {
      el.innerHTML = '<div class="ws-empty">无 workspace</div>';
      return;
    }
    el.innerHTML = ws.files.length
      ? ws.files.map(f => `<div class="ws-file"><span class="ws-path">${escapeHtml(f.path)}</span><span class="ws-size">${f.size}B</span></div>`).join('')
      : '<div class="ws-empty">workspace 为空</div>';
  } catch (e) {
    el.innerHTML = '<div class="ws-empty">加载失败</div>';
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

function showNewWorkerModal() { 
  $('new-worker-modal')?.classList.remove('hidden');
  // 加载 workspace 列表
  loadWorkspaceList();
  // 监听模式切换
  const modeSelect = $('new-worker-mode');
  if (modeSelect) {
    modeSelect.onchange = toggleProactiveOptions;
  }
  toggleProactiveOptions(); // 初始化状态
}
function hideNewWorkerModal() { $('new-worker-modal')?.classList.add('hidden'); }
window.hideNewWorkerModal = hideNewWorkerModal;

// 切换 workspace 类型
function toggleWorkspaceType() {
  const workspaceType = document.querySelector('input[name="workspace-type"]:checked')?.value || 'new';
  const newOptions = $('new-workspace-options');
  const existingOptions = $('existing-workspace-options');
  
  if (workspaceType === 'existing') {
    newOptions?.classList.add('hidden');
    existingOptions?.classList.remove('hidden');
  } else {
    newOptions?.classList.remove('hidden');
    existingOptions?.classList.add('hidden');
  }
}
window.toggleWorkspaceType = toggleWorkspaceType;

// 加载 workspace 列表
async function loadWorkspaceList() {
  const select = $('existing-workspace-select');
  if (!select) return;
  
  try {
    const data = await api('/api/workspaces');
    const workspaces = data.workspaces || [];
    
    if (workspaces.length === 0) {
      select.innerHTML = '<option value="">暂无可用 Workspace</option>';
      return;
    }
    
    select.innerHTML = '<option value="">选择 Workspace...</option>' +
      workspaces.map(ws => `
        <option value="${ws.name}" 
                data-cli="${ws.cli_type}" 
                data-roles="${ws.has_roles}">
          ${escapeHtml(ws.name)} (${ws.cli_type}${ws.has_roles ? ', 有角色配置' : ''})
        </option>
      `).join('');
    
    // 监听选择变化，显示详情
    select.onchange = () => showWorkspaceInfo(select.value);
  } catch (e) {
    console.error('加载 workspace 列表失败:', e);
    select.innerHTML = '<option value="">加载失败</option>';
  }
}

// 显示 workspace 信息
async function showWorkspaceInfo(workspaceName) {
  const infoEl = $('workspace-info');
  if (!infoEl || !workspaceName) {
    infoEl.innerHTML = '';
    return;
  }
  
  try {
    const wsData = await api(`/api/agents/${workspaceName}/workspace`);
    
    if (!wsData.exists) {
      infoEl.innerHTML = '<div class="error">Workspace 不存在</div>';
      return;
    }
    
    const fileCount = wsData.files?.length || 0;
    const hasRoles = wsData.files?.some(f => f.path === 'roles.md');
    const hasSubs = wsData.files?.some(f => f.path === 'subscriptions.json');
    
    infoEl.innerHTML = `
      <div class="workspace-info-item">
        <span class="workspace-info-label">路径:</span>
        <span class="workspace-info-value">${escapeHtml(wsData.workspace_dir)}</span>
      </div>
      <div class="workspace-info-item">
        <span class="workspace-info-label">文件数:</span>
        <span class="workspace-info-value">${fileCount}</span>
      </div>
      <div class="workspace-info-item">
        <span class="workspace-info-label">角色配置:</span>
        <span class="workspace-info-value">${hasRoles ? '✓ 有' : '✗ 无'}</span>
      </div>
      <div class="workspace-info-item">
        <span class="workspace-info-label">订阅配置:</span>
        <span class="workspace-info-value">${hasSubs ? '✓ 有' : '✗ 无'}</span>
      </div>
    `;
  } catch (e) {
    infoEl.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

async function createWorker() {
  const id = $('new-worker-id')?.value?.trim();
  if (!id) { showToast('请填写 Worker ID', 'error'); return; }
  
  const cliType = $('new-worker-cli')?.value || 'mock';
  const workspaceType = document.querySelector('input[name="workspace-type"]:checked')?.value || 'new';
  
  try {
    let payload = {
      mode: 'passive',  // 默认 passive 模式，从 config.json 读取
      cli_type: cliType
    };
    
    if (workspaceType === 'existing') {
      // 使用已有 workspace
      const existingWs = $('existing-workspace-select')?.value;
      if (!existingWs) {
        showToast('请选择一个 Workspace', 'error');
        return;
      }
      payload.use_existing_workspace = true;
      payload.existing_workspace_name = existingWs;
    } else {
      // 创建新 workspace
      payload.role = $('new-worker-role')?.value?.trim() || '';
      payload.system_prompt = $('new-worker-prompt')?.value?.trim() || '';
      
      const skillsStr = $('new-worker-skills')?.value?.trim();
      if (skillsStr) {
        payload.skills = skillsStr.split(',').map(s => s.trim()).filter(Boolean);
      }
    }
    
    // 调用创建 API
    await api(`/api/agents/${id}/create`, {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    
    // 清空表单
    $('new-worker-id').value = '';
    $('new-worker-role').value = '';
    $('new-worker-prompt').value = '';
    $('new-worker-skills').value = '';
    
    hideNewWorkerModal();
    showToast(`Worker ${id} 创建成功！`);
    
    // 延迟刷新列表
    setTimeout(refresh, 1500);
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

// ============================
// 实时聊天功能
// ============================

let selectedMentions = []; // 当前选中的提及成员
let channelMembers = []; // 当前频道的成员列表
let autocompleteIndex = -1; // 自动补全选中索引
let autocompleteVisible = false; // 自动补全是否可见

async function refreshLiveChat() {
  const messagesEl = $('live-chat-messages');
  const channelSelect = $('live-channel-select');
  
  if (!messagesEl) return;
  
  // 初始化频道选择器
  if (channelSelect && channelSelect.options.length <= 1) {
    try {
      const chs = await api('/api/channels');
      channelSelect.innerHTML = '<option value="">选择频道...</option>' + 
        chs.map(ch => `<option value="${ch.name}" ${state.liveChatChannel === ch.name ? 'selected' : ''}>${escapeHtml(ch.name)}</option>`).join('');
    } catch (e) {
      console.error('加载频道列表失败:', e);
    }
  }
  
  // 如果没有选择频道，显示提示
  if (!state.liveChatChannel) {
    messagesEl.innerHTML = '<div class="empty-state">选择一个频道开始观察实时聊天</div>';
    return;
  }
  
  try {
    // 获取频道成员和消息
    const [memberStatus, response] = await Promise.all([
      api(`/api/channels/${state.liveChatChannel}/member-status`).catch(() => null),
      api(`/api/channels/${state.liveChatChannel}/messages?limit=100`)
    ]);
    
    const msgs = response.messages || [];
    
    // 更新成员列表
    if (memberStatus && memberStatus.members) {
      channelMembers = memberStatus.members.map(m => m.agent_id);
      updateMentionButtons(channelMembers);
    }
    
    if (msgs.length === 0) {
      messagesEl.innerHTML = '<div class="empty-state">暂无消息</div>';
      return;
    }
    
    // 检查是否有新消息
    const newMessageId = msgs[msgs.length - 1]?.id;
    const hasNewMessages = newMessageId !== state.lastMessageId;
    
    if (hasNewMessages || !state.lastMessageId) {
      state.lastMessageId = newMessageId;
      
      // 渲染消息
      messagesEl.innerHTML = msgs.map(m => {
        const isSelf = m.from === 'god';
        const isSystem = m.type === 'system' || m.type === 'status_report';
        const isWorker = !isSelf && !isSystem;
        
        return `<div class="live-msg ${isSelf ? 'msg-self' : ''} ${isSystem ? 'msg-system' : ''} ${isWorker ? 'msg-worker' : ''}">
          <div class="live-msg-header">
            <span class="live-msg-from">${escapeHtml(m.from || '')}</span>
            <span class="live-msg-type">${m.type || 'text'}</span>
            <span class="live-msg-time">${fmtTime(m.ts)}</span>
          </div>
          <div class="live-msg-content">${formatMentions(escapeHtml(m.content || ''))}</div>
          ${(m.mentions || []).length ? `<div class="live-msg-mentions">@${m.mentions.join(', @')}</div>` : ''}
        </div>`;
      }).join('');
      
      // 自动滚动到底部
      if ($('auto-scroll-check')?.checked) {
        setTimeout(() => {
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }, 100);
      }
    }
  } catch (e) {
    messagesEl.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

// 更新提及按钮
function updateMentionButtons(members) {
  const container = $('mention-members');
  if (!container) return;
  
  container.innerHTML = members.map(member => `
    <button class="mention-btn" onclick="toggleMention('${member}')" data-member="${member}">
      @${escapeHtml(member)}
    </button>
  `).join('');
}

// 切换成员提及
function toggleMention(member) {
  const btn = document.querySelector(`.mention-btn[data-member="${member}"]`);
  const allBtn = document.querySelector('.mention-all');
  
  if (selectedMentions.includes(member)) {
    // 取消选择
    selectedMentions = selectedMentions.filter(m => m !== member);
    btn?.classList.remove('active');
  } else {
    // 添加选择
    selectedMentions.push(member);
    btn?.classList.add('active');
    // 取消 @All
    allBtn?.classList.remove('active');
  }
  
  updateMentionInput();
}

// 选择所有成员
function selectAllMentions() {
  const allBtn = document.querySelector('.mention-all');
  const memberBtns = document.querySelectorAll('.mention-btn[data-member]');
  
  if (selectedMentions.length === channelMembers.length) {
    // 取消全选
    selectedMentions = [];
    allBtn?.classList.remove('active');
    memberBtns.forEach(btn => btn.classList.remove('active'));
  } else {
    // 全选
    selectedMentions = [...channelMembers];
    allBtn?.classList.add('active');
    memberBtns.forEach(btn => btn.classList.add('active'));
  }
  
  updateMentionInput();
}
window.selectAllMentions = selectAllMentions;

// 更新输入框中的 @提及
function updateMentionInput() {
  const contentEl = $('live-content');
  if (!contentEl) return;
  
  // 在光标位置插入或更新 @提及
  const mentions = selectedMentions.length > 0 ? selectedMentions.join(', ') : '';
  
  // 如果输入框为空，添加 @提及前缀
  if (!contentEl.value.trim()) {
    if (mentions) {
      contentEl.value = `@${mentions} `;
    }
  } else {
    // 否则在开头添加（如果还没有）
    const currentText = contentEl.value;
    const mentionPattern = /^@[\w\s,]+ /;
    if (!mentionPattern.test(currentText) && mentions) {
      contentEl.value = `@${mentions} ${currentText}`;
    }
  }
}

// 格式化消息中的 @提及（高亮显示）
function formatMentions(text) {
  return text.replace(/@(\w+)/g, '<span class="mention-highlight">@$1</span>');
}

function selectLiveChannel(channelName) {
  state.liveChatChannel = channelName;
  state.lastMessageId = null; // 重置最后消息ID
  refreshLiveChat();
}

async function sendLiveMessage() {
  const from = $('live-from')?.value || 'god';
  const content = $('live-content')?.value;
  
  if (!state.liveChatChannel) {
    showToast('请先选择一个频道', 'error');
    return;
  }
  
  if (!content?.trim()) {
    showToast('请输入消息内容', 'error');
    return;
  }
  
  // 从内容中提取 @提及，或者使用选中的成员
  let mentions = selectedMentions.length > 0 ? [...selectedMentions] : [];
  
  // 如果内容中有 @提及，也提取出来
  const mentionMatches = content.match(/@(\w+)/g);
  if (mentionMatches) {
    mentionMatches.forEach(m => {
      const name = m.substring(1); // 去掉 @
      if (!mentions.includes(name)) {
        mentions.push(name);
      }
    });
  }
  
  try {
    await api(`/api/channels/${state.liveChatChannel}/messages`, {
      method: 'POST',
      body: JSON.stringify({ 
        from: from.trim(), 
        content: content.trim(), 
        type: 'mention',
        mentions: mentions
      }),
    });
    
    $('live-content').value = '';
    selectedMentions = []; // 清空选中
    document.querySelectorAll('.mention-btn').forEach(btn => btn.classList.remove('active'));
    
    // 立即刷新显示
    await refreshLiveChat();
    showToast('消息已发送');
  } catch (e) {
    showToast('发送失败: ' + e.message, 'error');
  }
}

// 实时聊天自动刷新
function startLiveChatRefresh() {
  stopLiveChatRefresh();
  state.liveChatTimer = setInterval(() => {
    if (state.currentView === 'live-chat' && state.liveChatChannel) {
      refreshLiveChat();
    }
  }, 2000); // 每2秒刷新一次
}

function stopLiveChatRefresh() {
  if (state.liveChatTimer) {
    clearInterval(state.liveChatTimer);
    state.liveChatTimer = null;
  }
}

// 监听频道选择变化
document.addEventListener('DOMContentLoaded', () => {
  const channelSelect = $('live-channel-select');
  if (channelSelect) {
    channelSelect.addEventListener('change', (e) => {
      selectLiveChannel(e.target.value);
    });
  }
  
  // Enter 键发送消息
  const liveContent = $('live-content');
  if (liveContent) {
    liveContent.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendLiveMessage();
      }
    });
  }
});

// 在视图切换时管理实时聊天刷新
const originalSwitchView = switchView;
switchView = function(view) {
  originalSwitchView(view);
  
  if (view === 'live-chat') {
    startLiveChatRefresh();
  } else {
    stopLiveChatRefresh();
  }
  
  // 切换到频道管理视图时加载列表
  if (view === 'channels') {
    refreshChannelList();
  }
};

// ============================
// @提及自动补全功能
// ============================

// 监听输入框的键盘事件
document.addEventListener('DOMContentLoaded', () => {
  const liveContent = $('live-content');
  const autocompleteEl = $('mention-autocomplete');
  
  if (liveContent && autocompleteEl) {
    // 输入事件 - 检测 @
    liveContent.addEventListener('input', (e) => {
      const cursorPos = liveContent.selectionStart;
      const text = liveContent.value;
      
      // 查找光标前的最后一个 @
      const beforeCursor = text.substring(0, cursorPos);
      const lastAt = beforeCursor.lastIndexOf('@');
      
      if (lastAt !== -1) {
        const afterAt = beforeCursor.substring(lastAt + 1);
        // 如果 @后面有空格或其他字符，不触发
        if (!afterAt.includes(' ') && !afterAt.includes('\n')) {
          showAutocomplete(afterAt);
          return;
        }
      }
      
      hideAutocomplete();
    });
    
    // 键盘事件 - 上下选择和 Enter 确认
    liveContent.addEventListener('keydown', (e) => {
      if (!autocompleteVisible) return;
      
      const items = autocompleteEl.querySelectorAll('.autocomplete-item');
      
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        autocompleteIndex = Math.min(autocompleteIndex + 1, items.length - 1);
        updateAutocompleteSelection(items);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        autocompleteIndex = Math.max(autocompleteIndex - 1, 0);
        updateAutocompleteSelection(items);
      } else if (e.key === 'Enter' || e.key === 'Tab') {
        if (autocompleteIndex >= 0 && items[autocompleteIndex]) {
          e.preventDefault();
          selectAutocompleteItem(items[autocompleteIndex].dataset.member);
        }
      } else if (e.key === 'Escape') {
        hideAutocomplete();
      }
    });
    
    // 点击外部关闭自动补全
    document.addEventListener('click', (e) => {
      if (!liveContent.contains(e.target) && !autocompleteEl.contains(e.target)) {
        hideAutocomplete();
      }
    });
  }
});

// 显示自动补全
function showAutocomplete(query) {
  const autocompleteEl = $('mention-autocomplete');
  if (!autocompleteEl) return;
  
  // 过滤匹配的成员
  const matches = channelMembers.filter(m => 
    m.toLowerCase().includes(query.toLowerCase())
  );
  
  if (matches.length === 0) {
    hideAutocomplete();
    return;
  }
  
  autocompleteIndex = 0;
  autocompleteVisible = true;
  
  autocompleteEl.innerHTML = matches.map((member, index) => `
    <div class="autocomplete-item ${index === 0 ? 'selected' : ''}" 
         data-member="${member}"
         onclick="selectAutocompleteItem('${member}')">
      <span class="member-name">@${escapeHtml(member)}</span>
    </div>
  `).join('');
  
  // 定位自动补全框
  const textarea = $('live-content');
  const rect = textarea.getBoundingClientRect();
  autocompleteEl.style.left = rect.left + 'px';
  autocompleteEl.style.top = (rect.bottom + 5) + 'px';
  autocompleteEl.style.width = rect.width + 'px';
  autocompleteEl.classList.add('show');
}

// 隐藏自动补全
function hideAutocomplete() {
  const autocompleteEl = $('mention-autocomplete');
  if (autocompleteEl) {
    autocompleteEl.classList.remove('show');
  }
  autocompleteVisible = false;
  autocompleteIndex = -1;
}

// 更新自动补全选中状态
function updateAutocompleteSelection(items) {
  items.forEach((item, index) => {
    item.classList.toggle('selected', index === autocompleteIndex);
  });
}

// 选择自动补全项
function selectAutocompleteItem(member) {
  const liveContent = $('live-content');
  if (!liveContent) return;
  
  const cursorPos = liveContent.selectionStart;
  const text = liveContent.value;
  
  // 找到最后一个 @的位置
  const beforeCursor = text.substring(0, cursorPos);
  const lastAt = beforeCursor.lastIndexOf('@');
  
  if (lastAt !== -1) {
    // 替换 @后面的文本为选中的成员
    const newText = text.substring(0, lastAt + 1) + member + ' ' + text.substring(cursorPos);
    liveContent.value = newText;
    
    // 设置光标位置
    const newCursorPos = lastAt + 1 + member.length + 1;
    liveContent.setSelectionRange(newCursorPos, newCursorPos);
    liveContent.focus();
  }
  
  hideAutocomplete();
}

// ============================
// 频道管理功能
// ============================

let currentChannelName = null; // 当前选中的频道

async function refreshChannelList() {
  const panel = $('channel-list-panel');
  if (!panel) return;
  
  try {
    const chs = await api('/api/channels');
    
    if (chs.length === 0) {
      panel.innerHTML = '<div class="empty-state">暂无频道<br><button class="btn btn-primary" onclick="showNewChannelModal()">+ 新建频道</button></div>';
      return;
    }
    
    panel.innerHTML = `
      <div class="channel-list">
        ${chs.map(ch => `
          <div class="channel-item ${currentChannelName === ch.name ? 'active' : ''}" 
               onclick="loadChannelDetail('${ch.name}')">
            <div class="channel-name">${escapeHtml(ch.name)}</div>
            <div class="channel-meta">
              <span>${ch.members?.length || 0} 成员</span>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (e) {
    panel.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

async function loadChannelDetail(name) {
  currentChannelName = name;
  const panel = $('channel-detail-panel');
  if (!panel) return;
  
  // 更新列表选中状态
  refreshChannelList();
  
  try {
    const [meta, msgs, agents] = await Promise.all([
      api(`/api/channels/${name}/meta`),
      api(`/api/channels/${name}/messages?limit=50`),
      api('/api/agents')  // 获取所有 Worker
    ]);
    
    const messages = msgs.messages || [];
    const workers = agents || [];  // Worker 列表
    
    panel.innerHTML = `
      <div class="channel-detail">
        <div class="channel-header">
          <h3>${escapeHtml(name)}</h3>
          <div class="channel-actions">
            <button class="btn btn-sm" onclick="clearChannelMessages('${name}')">清空消息</button>
          </div>
        </div>
        
        <div class="channel-info">
          <div class="info-row">
            <span class="label">最大消息数:</span>
            <div class="value-edit">
              <span class="value" id="max-msgs-display-${name}">${meta.max_messages || '无限制'}</span>
              <button class="btn btn-xs" onclick="editMaxMessages('${name}', ${meta.max_messages || 0})">编辑</button>
            </div>
          </div>
          <div class="info-row">
            <span class="label">成员数:</span>
            <span class="value">${meta.members?.length || 0}</span>
          </div>
          <div class="info-row">
            <span class="label">管理员:</span>
            <span class="value">${(meta.admins || []).join(', ') || '无'}</span>
          </div>
        </div>
        
        <div class="channel-members">
          <h4>成员管理</h4>
          <div class="member-list">
            ${(meta.members || []).map(m => `
              <div class="member-item">
                <span>${escapeHtml(m)}</span>
                <button class="btn btn-xs btn-danger" onclick="removeMemberFromChannel('${name}', '${m}')">移除</button>
              </div>
            `).join('')}
          </div>
          <div class="member-add">
            <select id="add-member-select-${name}" class="member-select">
              <option value="">选择 Worker...</option>
              ${workers.map(w => `
                <option value="${w.agent_id}" ${meta.members?.includes(w.agent_id) ? 'disabled' : ''}>
                  ${escapeHtml(w.agent_id)} ${meta.members?.includes(w.agent_id) ? '(已在频道)' : ''}
                </option>
              `).join('')}
            </select>
            <button class="btn btn-sm" onclick="addMemberToChannel('${name}')">+ 添加成员</button>
          </div>
        </div>
        
        <div class="channel-admins">
          <h4>管理员管理</h4>
          <div class="admin-list">
            ${(meta.admins || []).map(a => `
              <div class="admin-item">
                <span>${escapeHtml(a)}</span>
              </div>
            `).join('')}
          </div>
          <div class="admin-add">
            <input type="text" id="add-admin-input-${name}" placeholder="输入 admin_id">
            <label class="checkbox-label">
              <input type="checkbox" id="add-admin-human-${name}"> 人类管理员
            </label>
            <button class="btn btn-sm" onclick="addAdminToChannel('${name}')">+ 添加管理员</button>
          </div>
        </div>
        
        <div class="channel-messages-preview">
          <h4>最近消息 (${messages.length})</h4>
          <div class="messages-list">
            ${messages.slice(-10).map(m => `
              <div class="message-preview">
                <span class="msg-from">${escapeHtml(m.from)}</span>
                <span class="msg-content">${escapeHtml(m.content.substring(0, 50))}${m.content.length > 50 ? '...' : ''}</span>
                <span class="msg-time">${fmtTime(m.ts)}</span>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  } catch (e) {
    panel.innerHTML = `<div class="error">加载失败: ${escapeHtml(e.message)}</div>`;
  }
}

async function addMemberToChannel(channelName) {
  const select = $(`add-member-select-${channelName}`);
  if (!select || !select.value) {
    showToast('请选择一个 Worker', 'error');
    return;
  }
  
  try {
    await api(`/api/channels/${channelName}/members`, {
      method: 'POST',
      body: JSON.stringify({ agent_id: select.value })
    });
    showToast('成员已添加');
    loadChannelDetail(channelName);
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

async function removeMemberFromChannel(channelName, memberId) {
  if (!confirm(`确定要从频道 ${channelName} 中移除成员 ${memberId} 吗？`)) return;
  
  try {
    await api(`/api/channels/${channelName}/members/${memberId}`, {
      method: 'DELETE'
    });
    showToast('成员已移除');
    loadChannelDetail(channelName);
  } catch (e) {
    showToast('移除失败: ' + e.message, 'error');
  }
}

async function addAdminToChannel(channelName) {
  const input = $(`add-admin-input-${channelName}`);
  const isHuman = $(`add-admin-human-${channelName}`)?.checked || false;
  
  if (!input || !input.value.trim()) {
    showToast('请输入管理员 ID', 'error');
    return;
  }
  
  try {
    await api(`/api/channels/${channelName}/admins`, {
      method: 'POST',
      body: JSON.stringify({ 
        agent_id: input.value.trim(), 
        is_human: isHuman 
      })
    });
    showToast('管理员已添加');
    loadChannelDetail(channelName);
  } catch (e) {
    showToast('添加失败: ' + e.message, 'error');
  }
}

async function clearChannelMessages(channelName) {
  if (!confirm(`确定要清空频道 ${channelName} 的所有消息吗？`)) return;
  
  try {
    await api(`/api/channels/${channelName}/messages`, { method: 'DELETE' });
    showToast('消息已清空');
    loadChannelDetail(channelName);
  } catch (e) {
    showToast('清空失败: ' + e.message, 'error');
  }
}

async function editMaxMessages(channelName, currentValue) {
  const newValue = prompt(
    `设置频道 "${channelName}" 的最大消息数:\n` +
    `(当前: ${currentValue || '无限制'}, 0 = 无限制)`,
    currentValue || 0
  );
  
  if (newValue === null) return; // 用户取消
  
  const maxMsgs = parseInt(newValue);
  if (isNaN(maxMsgs) || maxMsgs < 0) {
    showToast('请输入有效的数字（>= 0）', 'error');
    return;
  }
  
  try {
    await api(`/api/channels/${channelName}/config`, {
      method: 'PUT',
      body: JSON.stringify({ max_messages: maxMsgs })
    });
    
    showToast(`最大消息数已更新为: ${maxMsgs || '无限制'}`);
    loadChannelDetail(channelName);
  } catch (e) {
    showToast('更新失败: ' + e.message, 'error');
  }
}
window.editMaxMessages = editMaxMessages;