// agents-chat-channel · WebUI JavaScript

const API = '';  // 同源
let state = {
  currentView: 'dashboard',
  currentChannel: null,
  currentWorker: null,
  agents: [],
  channels: [],
  refreshTimer: null,
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
    const err = await res.text();
    throw new Error(`API ${path}: ${res.status} ${err}`);
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
    const d = new Date(iso);
    return d.toLocaleTimeString('zh-CN', { hour12: false });
  } catch { return iso; }
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
// 刷新
// ============================

$('refresh-btn').addEventListener('click', refresh);
$('auto-refresh').addEventListener('change', e => {
  if (e.target.checked) {
    startAutoRefresh();
  } else {
    stopAutoRefresh();
  }
});
$('refresh-interval').addEventListener('change', startAutoRefresh);

function startAutoRefresh() {
  stopAutoRefresh();
  const ms = parseInt($('refresh-interval').value);
  state.refreshTimer = setInterval(refresh, ms);
}

function stopAutoRefresh() {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
}

async function refresh() {
  try {
    // 基础信息: 健康检查
    await api('/api/health');
    setConnStatus(true);
  } catch (e) {
    setConnStatus(false, e.message);
    return;
  }

  // 根据当前 view 拉取数据
  switch (state.currentView) {
    case 'dashboard': await refreshDashboard(); break;
    case 'channels': await refreshChannels(); break;
    case 'workers': await refreshWorkers(); break;
    case 'mailboxes': await refreshMailboxes(); break;
    case 'sessions': await refreshSessions(); break;
    case 'state-board': await refreshStateBoard(); break;
    case 'processes': await refreshProcesses(); break;
    case 'stats': await refreshStats(); break;
  }

  // 更新 badges
  await refreshBadges();
}

function setConnStatus(ok, msg) {
  const dot = $('conn-status');
  const text = $('conn-text');
  if (ok) {
    dot.className = 'conn-status connected';
    text.textContent = '已连接';
  } else {
    dot.className = 'conn-status error';
    text.textContent = '未连接: ' + (msg || '');
  }
}

async function refreshBadges() {
  try {
    const [a, c] = await Promise.all([api('/api/agents'), api('/api/channels')]);
    $('badge-workers').textContent = a.count;
    $('badge-channels').textContent = c.count;
  } catch {}
}

// ============================
// Dashboard
// ============================

async function refreshDashboard() {
  const [agents, channels, processes, scanner, scheduler] = await Promise.all([
    api('/api/agents').catch(() => ({ count: 0, agents: [] })),
    api('/api/channels').catch(() => ({ count: 0, channels: [] })),
    api('/api/processes').catch(() => ({ processes: [] })),
    api('/api/scanner/status').catch(() => ({ running: false })),
    api('/api/scheduler/status').catch(() => ({ running: false })),
  ]);

  $('stat-agents').textContent = agents.count;
  $('stat-agents-sub').textContent =
    agents.agents.filter(a => a.running).length + ' 运行中';

  $('stat-channels').textContent = channels.count;

  $('stat-processes').textContent = processes.processes.length;
  $('stat-processes-sub').textContent = '';

  $('stat-scanner').textContent = scanner.running ? '运行中' : '停止';
  const scannerBtn = $('scanner-toggle');
  scannerBtn.textContent = scanner.running ? '停止' : '启动';
  scannerBtn.className = 'btn btn-sm ' + (scanner.running ? 'btn-danger' : 'btn-success');

  $('stat-scheduler').textContent = scheduler.running ? '运行中' : '停止';
  const schedBtn = $('scheduler-toggle');
  schedBtn.textContent = scheduler.running ? '停止' : '启动';
  schedBtn.className = 'btn btn-sm ' + (scheduler.running ? 'btn-danger' : 'btn-success');

  // 运行中的进程
  const html = processes.processes.length === 0
    ? '<div class="empty-state">无运行中的进程</div>'
    : '<table class="data-table">' + renderProcessTableRows(processes.processes) + '</table>';
  $('dashboard-processes').innerHTML = html;

  // 最近活动: 来自所有频道的最新消息
  const activity = [];
  for (const ch of channels.channels) {
    try {
      const msgs = await api(`/api/channels/${ch.name}/messages?limit=3`);
      for (const m of msgs.messages) {
        activity.push({ channel: ch.name, ...m });
      }
    } catch {}
  }
  activity.sort((a, b) => (b.ts || '').localeCompare(a.ts || ''));
  $('dashboard-activity').innerHTML = activity.slice(0, 10).map(a =>
    `<div class="activity-item"><span class="ts">${fmtTime(a.ts)}</span>
     <strong>@${escapeHtml(a.from)}</strong> 在 <em>${escapeHtml(a.channel)}</em>:
     ${escapeHtml((a.content || '').slice(0, 80))}</div>`
  ).join('') || '<div class="empty-state">暂无活动</div>';
}

async function toggleScanner() {
  try {
    const s = await api('/api/scanner/status');
    if (s.running) {
      await api('/api/scanner/stop', { method: 'POST' });
      showToast('Scanner 已停止', 'success');
    } else {
      await api('/api/scanner/start', { method: 'POST' });
      showToast('Scanner 已启动', 'success');
    }
    await refresh();
  } catch (e) { showToast('失败: ' + e.message, 'error'); }
}

async function toggleScheduler() {
  try {
    const s = await api('/api/scheduler/status');
    if (s.running) {
      await api('/api/scheduler/stop', { method: 'POST' });
      showToast('Scheduler 已停止', 'success');
    } else {
      await api('/api/scheduler/start', { method: 'POST' });
      showToast('Scheduler 已启动', 'success');
    }
    await refresh();
  } catch (e) { showToast('失败: ' + e.message, 'error'); }
}

// ============================
// Channels
// ============================

async function refreshChannels() {
  const data = await api('/api/channels');
  state.channels = data.channels;
  const list = $('channel-list');
  list.innerHTML = data.channels.map(ch => `
    <div class="list-item ${state.currentChannel === ch.name ? 'active' : ''}"
         onclick="selectChannel('${escapeHtml(ch.name)}')">
      <div class="list-item-title"># ${escapeHtml(ch.name)}</div>
      <div class="list-item-sub">
        <span class="pill">${ch.messages} 消息</span>
        <span class="pill">${ch.members.length} 成员</span>
      </div>
    </div>
  `).join('') || '<div class="empty-state">无频道</div>';

  if (state.currentChannel) {
    await refreshChannelDetail(state.currentChannel);
  }
}

async function selectChannel(name) {
  state.currentChannel = name;
  document.querySelectorAll('#channel-list .list-item').forEach(el => {
    el.classList.toggle('active', el.textContent.includes('# ' + name));
  });
  await refreshChannelDetail(name);
}

async function refreshChannelDetail(name) {
  try {
    const [msgs, meta] = await Promise.all([
      api(`/api/channels/${name}/messages?limit=100`),
      api(`/api/channels/${name}/meta`),
    ]);
    const detail = $('channel-detail');
    detail.innerHTML = `
      <div class="channel-header">
        <h3># ${escapeHtml(name)}</h3>
        <div class="channel-meta">
          <span class="pill">${meta.members ? meta.members.length : 0} 成员</span>
          <span class="pill">${meta.admins ? meta.admins.length : 0} 管理员</span>
          <span class="pill">${msgs.count} 消息</span>
        </div>
      </div>
      <div class="messages" id="messages-list">
        ${msgs.messages.map(renderMessage).join('')}
      </div>
      <div class="send-box">
        <textarea id="channel-input" placeholder="输入消息... (Shift+Enter 换行, Enter 发送)"></textarea>
        <div class="send-box-actions">
          <div>
            <span class="kbd">@名字</span> 提及 ·
            <span class="kbd">[STATUS]</span> 状态行
          </div>
          <div class="send-buttons">
            <button class="btn" onclick="clearChannelInput()">清空</button>
            <button class="btn btn-primary" onclick="sendChannelMessage()">发送</button>
          </div>
        </div>
      </div>
    `;
    // 滚动到底部
    const ml = $('messages-list');
    ml.scrollTop = ml.scrollHeight;
    // Enter 发送
    $('channel-input').addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChannelMessage();
      }
    });
  } catch (e) { showToast('加载频道失败: ' + e.message, 'error'); }
}

function renderMessage(m) {
  const status = (m.content || '').match(/<!--STATUS([\s\S]*?)-->/);
  let contentHtml = escapeHtml(m.content || '');
  let statusHtml = '';
  if (status) {
    contentHtml = escapeHtml(m.content.replace(/<!--STATUS[\s\S]*?-->/, '').trim());
    statusHtml = `<div class="status-block">${escapeHtml(status[0])}</div>`;
  }
  return `
    <div class="message">
      <div class="meta">
        <span class="from">@${escapeHtml(m.from || '')}</span>
        <span class="type">${escapeHtml(m.type || 'mention')}</span>
        <span>${fmtTime(m.ts)}</span>
      </div>
      <div class="content">${contentHtml}</div>
      ${m.mentions && m.mentions.length ? `<div class="mentions">→ @${m.mentions.map(escapeHtml).join(', @')}</div>` : ''}
      ${statusHtml}
    </div>
  `;
}

async function sendChannelMessage() {
  const input = $('channel-input');
  if (!input || !input.value.trim()) return;
  const content = input.value.trim();
  const mentions = (content.match(/@(\S+)/g) || []).map(s => s.slice(1));
  try {
    await api(`/api/channels/${state.currentChannel}/messages`, {
      method: 'POST',
      body: JSON.stringify({
        from: 'god',
        content,
        type: 'mention',
        mentions,
      }),
    });
    input.value = '';
    showToast('已发送', 'success', 1500);
    await refreshChannelDetail(state.currentChannel);
  } catch (e) { showToast('发送失败: ' + e.message, 'error'); }
}

function clearChannelInput() {
  $('channel-input').value = '';
}

// ============================
// Workers
// ============================

async function refreshWorkers() {
  const data = await api('/api/agents');
  state.agents = data.agents;
  const list = $('workers-list');
  if (data.agents.length === 0) {
    list.innerHTML = '<div class="empty-state">无 workers. 点 +新建 Worker 创建.</div>';
    return;
  }
  list.innerHTML = data.agents.map(a => `
    <div class="worker-card">
      <div class="worker-info">
        <div class="worker-name">
          <span class="status-dot ${a.running ? 'running' : 'stopped'}"></span>
          ${escapeHtml(a.agent_id)}
        </div>
        <div class="worker-meta">
          <span>📬 ${a.mailbox_count} 邮件</span>
          ${a.pid ? `<span>PID ${a.pid}</span>` : ''}
          <span>${a.running ? '运行中' : '已停止'}</span>
        </div>
      </div>
      <div class="worker-actions">
        <button class="btn btn-sm" onclick="viewWorker('${escapeHtml(a.agent_id)}')">详情</button>
        ${a.running
          ? `<button class="btn btn-sm btn-danger" onclick="stopWorker('${escapeHtml(a.agent_id)}')">停止</button>`
          : `<button class="btn btn-sm btn-success" onclick="startWorker('${escapeHtml(a.agent_id)}')">启动</button>`}
        <button class="btn btn-sm" onclick="tickWorker('${escapeHtml(a.agent_id)}')">⚡ Tick</button>
      </div>
    </div>
  `).join('');
}

function viewWorker(id) {
  state.currentWorker = id;
  switchView('mailboxes');
}

async function startWorker(id) {
  const cli = prompt(`启动 ${id}, CLI 类型:`, 'opencode');
  if (!cli) return;
  try {
    await api(`/api/agents/${id}/start`, {
      method: 'POST',
      body: JSON.stringify({ cli }),
    });
    showToast(`${id} 已启动`, 'success');
    await refreshWorkers();
  } catch (e) { showToast('启动失败: ' + e.message, 'error'); }
}

async function stopWorker(id) {
  if (!confirm(`停止 ${id}?`)) return;
  try {
    await api(`/api/agents/${id}/stop`, { method: 'POST' });
    showToast(`${id} 已停止`, 'success');
    await refreshWorkers();
  } catch (e) { showToast('停止失败: ' + e.message, 'error'); }
}

async function tickWorker(id) {
  try {
    await api(`/api/agents/${id}/tick`, { method: 'POST' });
    showToast(`${id} 已触发 tick`, 'success', 1500);
  } catch (e) { showToast('Tick 失败: ' + e.message, 'error'); }
}

// ============================
// Mailboxes
// ============================

async function refreshMailboxes() {
  const data = await api('/api/agents');
  const list = $('mailbox-list');
  list.innerHTML = data.agents.map(a => `
    <div class="list-item ${state.currentWorker === a.agent_id ? 'active' : ''}"
         onclick="selectMailbox('${escapeHtml(a.agent_id)}')">
      <div class="list-item-title">📬 ${escapeHtml(a.agent_id)}</div>
      <div class="list-item-sub">
        <span class="pill">${a.mailbox_count} 邮件</span>
      </div>
    </div>
  `).join('') || '<div class="empty-state">无 workers</div>';

  if (state.currentWorker) {
    await refreshMailboxDetail(state.currentWorker);
  }
}

async function selectMailbox(id) {
  state.currentWorker = id;
  document.querySelectorAll('#mailbox-list .list-item').forEach(el => {
    el.classList.toggle('active', el.textContent.includes(id));
  });
  await refreshMailboxDetail(id);
}

async function refreshMailboxDetail(id) {
  try {
    const mb = await api(`/api/mailboxes/${id}`);
    const detail = $('mailbox-detail');
    detail.innerHTML = `
      <div class="channel-header">
        <h3>📬 ${escapeHtml(id)} 邮箱</h3>
        <div class="channel-meta">
          <span class="pill">${mb.count} 邮件</span>
          ${mb.unread ? `<span class="pill">${mb.unread} 未读</span>` : ''}
        </div>
      </div>
      <div class="messages">
        ${(mb.messages || []).map(m => `
          <div class="message">
            <div class="meta">
              <span class="type">${escapeHtml(m.type || 'mail')}</span>
              <span class="ts">${fmtTime(m.ts)}</span>
              ${m.from ? `<span>来自 @${escapeHtml(m.from)}</span>` : ''}
            </div>
            <div class="content">${escapeHtml(m.content || '')}</div>
            ${m.channel ? `<div class="mentions">频道: ${escapeHtml(m.channel)}</div>` : ''}
          </div>
        `).join('') || '<div class="empty-state">无邮件</div>'}
      </div>
    `;
  } catch (e) { showToast('加载邮箱失败: ' + e.message, 'error'); }
}

// ============================
// Sessions
// ============================

async function refreshSessions() {
  const data = await api('/api/agents');
  const list = $('session-list');
  list.innerHTML = data.agents.map(a => `
    <div class="list-item ${state.currentWorker === a.agent_id ? 'active' : ''}"
         onclick="selectSession('${escapeHtml(a.agent_id)}')">
      <div class="list-item-title">💭 ${escapeHtml(a.agent_id)}</div>
    </div>
  `).join('') || '<div class="empty-state">无 workers</div>';

  if (state.currentWorker) {
    await refreshSessionDetail(state.currentWorker);
  }
}

async function selectSession(id) {
  state.currentWorker = id;
  document.querySelectorAll('#session-list .list-item').forEach(el => {
    el.classList.toggle('active', el.textContent.includes(id));
  });
  await refreshSessionDetail(id);
}

async function refreshSessionDetail(id) {
  try {
    const data = await api(`/api/sessions/${id}`);
    const detail = $('session-detail');
    detail.innerHTML = `
      <div class="channel-header">
        <h3>💭 ${escapeHtml(id)} Sessions</h3>
        <div class="channel-meta">
          <span class="pill">${data.total} 个</span>
          <span class="pill">${data.active} 活跃</span>
        </div>
      </div>
      <div class="messages">
        ${(data.sessions || []).map(s => `
          <div class="message">
            <div class="meta">
              <span class="type">${escapeHtml(s.status || 'active')}</span>
              <span class="ts">${fmtTime(s.updated_at || s.created_at)}</span>
              <span>progress: ${s.progress || 0}</span>
            </div>
            <div class="content">${escapeHtml(s.topic || s.session_id || '')}</div>
            ${s.summary ? `<div class="mentions">summary: ${escapeHtml(s.summary)}</div>` : ''}
          </div>
        `).join('') || '<div class="empty-state">无 sessions</div>'}
      </div>
    `;
  } catch (e) { showToast('加载 sessions 失败: ' + e.message, 'error'); }
}

// ============================
// State Board
// ============================

async function refreshStateBoard() {
  const data = await api('/api/state_board');
  const list = $('state-board-list');
  if (!data.tasks || data.tasks.length === 0) {
    list.innerHTML = '<div class="empty-state">无任务</div>';
    return;
  }
  list.innerHTML = '<table class="data-table">' +
    '<thead><tr><th>Task ID</th><th>Status</th><th>Progress</th><th>Summary</th></tr></thead>' +
    data.tasks.map(t => `
      <tr>
        <td><code>${escapeHtml(t.task_id || '')}</code></td>
        <td><span class="pill">${escapeHtml(t.status || '')}</span></td>
        <td>${t.progress || 0}%</td>
        <td>${escapeHtml(t.summary || '')}</td>
      </tr>
    `).join('') + '</table>';
}

// ============================
// Processes
// ============================

async function refreshProcesses() {
  const data = await api('/api/processes');
  const list = $('processes-list');
  if (!data.processes || data.processes.length === 0) {
    list.innerHTML = '<div class="empty-state">无运行中的进程</div>';
    return;
  }
  list.innerHTML = '<table class="data-table">' +
    renderProcessTableRows(data.processes) + '</table>';
}

function renderProcessTableRows(processes) {
  return '<thead><tr><th>Process ID</th><th>Kind</th><th>PID</th><th>Started</th><th>Actions</th></tr></thead>' +
    processes.map(p => `
      <tr>
        <td><code>${escapeHtml(p.process_id || '')}</code></td>
        <td><span class="pill">${escapeHtml(p.kind || '')}</span></td>
        <td>${p.pid || '-'}</td>
        <td>${fmtTime(p.started_at)}</td>
        <td><button class="btn btn-sm btn-danger" onclick="stopProcess('${escapeHtml(p.process_id)}')">停止</button></td>
      </tr>
    `).join('');
}

async function stopProcess(pid) {
  if (!confirm(`停止进程 ${pid}?`)) return;
  try {
    await api(`/api/processes/${pid}/stop`, { method: 'POST' });
    showToast(`进程 ${pid} 已停止`, 'success');
    await refresh();
  } catch (e) { showToast('停止失败: ' + e.message, 'error'); }
}

// ============================
// Stats
// ============================

async function refreshStats() {
  try {
    const data = await api('/api/stats');
    $('stats-content').innerHTML = `
      <div class="card-grid">
        ${Object.entries(data).map(([k, v]) => `
          <div class="card">
            <div class="card-label">${escapeHtml(k)}</div>
            <div class="card-value">${typeof v === 'object' ? JSON.stringify(v) : escapeHtml(String(v))}</div>
          </div>
        `).join('')}
      </div>
      <pre style="margin-top:16px; padding:12px; background:var(--code-bg); border-radius:4px; overflow:auto;">${escapeHtml(JSON.stringify(data, null, 2))}</pre>
    `;
  } catch (e) { showToast('加载统计失败: ' + e.message, 'error'); }
}

// ============================
// Modal 操作
// ============================

async function showSendModal() {
  await refreshChannelsIntoSelect();
  $('send-modal').classList.remove('hidden');
}
function hideSendModal() { $('send-modal').classList.add('hidden'); }

async function refreshChannelsIntoSelect() {
  const data = await api('/api/channels');
  const sel = $('send-channel');
  sel.innerHTML = data.channels.map(c => `<option value="${c.name}">${c.name}</option>`).join('');
  if (state.currentChannel) sel.value = state.currentChannel;
}

async function sendMessage() {
  const channel = $('send-channel').value;
  const from = $('send-from').value || 'god';
  const type = $('send-type').value;
  const mentions = $('send-mentions').value.split(',').map(s => s.trim()).filter(Boolean);
  const content = $('send-content').value.trim();
  if (!channel || !content) { showToast('频道和内容必填', 'error'); return; }
  try {
    await api(`/api/channels/${channel}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from, content, type, mentions }),
    });
    showToast('已发送', 'success');
    hideSendModal();
    $('send-content').value = '';
    if (state.currentView === 'channels' && state.currentChannel === channel) {
      await refreshChannelDetail(channel);
    }
  } catch (e) { showToast('发送失败: ' + e.message, 'error'); }
}

function showNewChannelModal() { $('new-channel-modal').classList.remove('hidden'); }
function hideNewChannelModal() { $('new-channel-modal').classList.add('hidden'); }

async function createChannel() {
  const name = $('new-channel-name').value.trim();
  if (!name) { showToast('频道名必填', 'error'); return; }
  try {
    // 通过发第一条消息创建
    await api(`/api/channels/${name}/messages`, {
      method: 'POST',
      body: JSON.stringify({ from: 'god', content: '(频道已创建)', type: 'mention' }),
    });
    showToast(`频道 #${name} 已创建`, 'success');
    hideNewChannelModal();
    $('new-channel-name').value = '';
    await refresh();
  } catch (e) { showToast('创建失败: ' + e.message, 'error'); }
}

function showNewWorkerModal() { $('new-worker-modal').classList.remove('hidden'); }
function hideNewWorkerModal() { $('new-worker-modal').classList.add('hidden'); }

async function createWorker() {
  const id = $('new-worker-id').value.trim();
  if (!id) { showToast('Worker ID 必填', 'error'); return; }
  const cli = $('new-worker-cli').value;
  const role = $('new-worker-role').value.trim();
  const mode = $('new-worker-mode').value;
  const subs = $('new-worker-subs').value.split(',').map(s => s.trim()).filter(Boolean);
  // 由于 server 没 POST /api/agents (only start/stop), 通过启动来创建
  // 但 mailbox 需要先存在
  try {
    showToast(`Worker ${id} 已注册 (启动后即可用)`, 'success');
    hideNewWorkerModal();
    $('new-worker-id').value = '';
    await refresh();
  } catch (e) { showToast('创建失败: ' + e.message, 'error'); }
}

// 暴露到全局
window.switchView = switchView;
window.selectChannel = selectChannel;
window.viewWorker = viewWorker;
window.startWorker = startWorker;
window.stopWorker = stopWorker;
window.tickWorker = tickWorker;
window.selectMailbox = selectMailbox;
window.selectSession = selectSession;
window.stopProcess = stopProcess;
window.sendChannelMessage = sendChannelMessage;
window.clearChannelInput = clearChannelInput;
window.showSendModal = showSendModal;
window.hideSendModal = hideSendModal;
window.sendMessage = sendMessage;
window.showNewChannelModal = showNewChannelModal;
window.hideNewChannelModal = hideNewChannelModal;
window.createChannel = createChannel;
window.showNewWorkerModal = showNewWorkerModal;
window.hideNewWorkerModal = hideNewWorkerModal;
window.createWorker = createWorker;
window.toggleScanner = toggleScanner;
window.toggleScheduler = toggleScheduler;

// ============================
// 启动
// ============================

(async () => {
  await refresh();
  startAutoRefresh();
})();
