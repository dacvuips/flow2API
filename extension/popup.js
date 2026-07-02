/** Flow2API — Popup UI */

const $ = (id) => document.getElementById(id);

function send(type, payload = {}, timeoutMs = 10000) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(undefined), timeoutMs);
    chrome.runtime.sendMessage({ type, ...payload }, (response) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) {
        resolve(undefined);
        return;
      }
      resolve(response);
    });
  });
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value ?? '-';
}

function formatTokenAge(ms) {
  if (ms === null || ms === undefined) return 'chưa có';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 10) return 'vừa nhận';
  if (seconds < 60) return `${seconds} giây trước`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} phút trước`;
  return `${Math.floor(seconds / 3600)} giờ trước`;
}

function setConnection(status) {
  const pill = $('status-pill');
  const text = $('status-text');
  if (!pill || !text) return;
  pill.className = 'status';

  if (status.manualDisconnect || status.paused) {
    pill.classList.add('paused');
    text.textContent = 'tạm dừng';
    return;
  }

  if (!status.connected) {
    pill.classList.add('offline');
    text.textContent = 'ngoại tuyến';
    return;
  }

  if (status.state === 'running') {
    pill.classList.add('running');
    text.textContent = 'đang chạy';
    return;
  }

  text.textContent = 'đã kết nối';
}

function renderStatus(status) {
  if (!status) return;
  setConnection(status);

  const tokenRow = $('token-row');
  if (tokenRow) {
    tokenRow.textContent = status.flowKeyPresent ? formatTokenAge(status.tokenAge) : 'chưa có';
    tokenRow.className = status.flowKeyPresent ? 'value token-ready' : 'value token-missing';
  }

  const prof = $('profile-row');
  if (prof) {
    const pid = status.profileId || '';
    prof.textContent = pid ? `${pid.slice(0, 8)}…` : '—';
    prof.title = pid || '';
  }
  setText('account-row', status.userInfo?.email || status.userInfo?.name || 'chưa nhận diện');

  const metrics = status.metrics || {};
  setText('requests-num', metrics.requestCount || 0);
  setText('success-num', metrics.successCount || 0);
  setText('failed-num', metrics.failedCount || 0);
}

function fetchStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS' }, (reply) => {
    if (chrome.runtime.lastError) return;
    renderStatus(reply);
  });
}

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === 'STATUS_PUSH' || message.type === 'REQUEST_LOG_UPDATE') fetchStatus();
});

async function loadAutoClickSetting() {
  const res = await send('GET_AUTO_CLICK_CREATE');
  const input = $('auto-click-create');
  if (input) input.checked = res?.enabled !== false;
  renderAutoClickStatus(res?.last);
}

function renderAutoClickStatus(last) {
  const el = $('auto-click-status');
  if (!el) return;
  if (!last?.message) {
    el.textContent = '—';
    el.className = 'value';
    return;
  }
  const sec = Math.max(0, Math.floor((Date.now() - (last.at || 0)) / 1000));
  const suffix = sec <= 90 ? '' : ` (${Math.floor(sec / 60)}p trước)`;
  el.textContent = `${last.message}${suffix}`;
  if (last.status === 'success') el.className = 'value token-ready';
  else el.className = 'value';
}

$('auto-click-create')?.addEventListener('change', async (e) => {
  await send('SET_AUTO_CLICK_CREATE', { enabled: e.target.checked });
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && changes.autoClickLastStatus) {
    renderAutoClickStatus(changes.autoClickLastStatus.newValue);
  }
});

fetchStatus();
loadAutoClickSetting();
setInterval(fetchStatus, 1500);
