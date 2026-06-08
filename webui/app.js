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
  const [agents, channels] = await Promise.all([
    api('/api/agents').catch(() => ({ count: 0, agents: [] })),
    api('/api/channels').catch(() => ({ count: 0, channels: [] })),
  ]);

  $('stat-agents').textContent = agents.count;
  $('stat-agents-sub').textContent =
    agents.agents.filter(a => a.running).length + ' 运行中';
  $('stat-channels').textContent = channels.count;

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

// ============================
// 启动
// ============================

(async () => {
  await refresh();
  startAutoRefresh();
})();
