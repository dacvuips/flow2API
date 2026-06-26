/** Flow2API — Popup UI */

const $ = (id) => document.getElementById(id);

let selectedClearSec = 10;
let clearCountdownTimer = null;

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

function formatIntervalLabel(sec) {
  if (sec >= 60 && sec % 60 === 0) return `${sec / 60}p`;
  return `${sec}s`;
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

async function getTargetTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]?.id) return tabs[0];
  tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('tabStatus').classList.toggle('active', name === 'status');
  $('tabStatus').hidden = name !== 'status';
  $('tabClear').classList.toggle('active', name === 'clear');
  $('tabClear').hidden = name !== 'clear';

  if (name === 'clear') {
    refreshClearState();
    startClearCountdownTicker();
  } else {
    stopClearCountdownTicker();
  }

  chrome.storage.local.set({ f2apiActiveTab: name }).catch(() => {});
}

document.querySelectorAll('.tab-btn').forEach((btn) => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

function highlightClearPreset(sec) {
  selectedClearSec = sec;
  document.querySelectorAll('.clear-preset-btn').forEach((btn) => {
    btn.classList.toggle('active', parseInt(btn.dataset.sec, 10) === sec);
  });
  const input = $('clear-custom-input');
  if (document.activeElement !== input) input.value = String(sec);
}

function resetClearStartButton(running = false) {
  const btn = $('btn-clear-start');
  btn.dataset.busy = '0';
  btn.textContent = running ? '↻ CHẠY LẠI' : '▶ BẮT ĐẦU';
}

function renderClear(cs) {
  if (!cs) return;
  selectedClearSec = cs.intervalSec ?? selectedClearSec;

  setText('clear-count', cs.clearCount ?? 0);
  setText('clear-interval-label', formatIntervalLabel(cs.intervalSec ?? 10));

  const running = !!cs.running;
  const dot = $('clear-status-dot');
  const statusText = $('clear-status-text');
  if (dot) dot.style.background = running ? 'var(--green)' : 'var(--muted)';
  if (statusText) statusText.textContent = running ? 'Đang chạy' : 'Đã tắt';

  const startBtn = $('btn-clear-start');
  const stopBtn = $('btn-clear-stop');
  startBtn.disabled = false;
  startBtn.textContent = running ? '↻ CHẠY LẠI' : '▶ BẮT ĐẦU';
  stopBtn.disabled = !running;

  highlightClearPreset(cs.intervalSec ?? selectedClearSec);
  updateClearCountdownDisplay(cs);
}

function updateClearCountdownDisplay(cs) {
  const el = $('clear-countdown');
  if (!el) return;
  if (!cs?.running) {
    el.textContent = '—';
    return;
  }
  el.textContent = cs.secondsUntilNext != null ? `${cs.secondsUntilNext}s` : '…';
}

function startClearCountdownTicker() {
  stopClearCountdownTicker();
  clearCountdownTimer = setInterval(async () => {
    if ($('tabClear').hidden) return;
    const cs = await send('GET_CLEAR_STATE');
    if (cs) updateClearCountdownDisplay(cs);
  }, 1000);
}

function stopClearCountdownTicker() {
  if (clearCountdownTimer) {
    clearInterval(clearCountdownTimer);
    clearCountdownTimer = null;
  }
}

async function refreshClearState() {
  const cs = await send('GET_CLEAR_STATE');
  if (cs) renderClear(cs);

  const tab = await getTargetTab();
  const hint = $('clear-hint');
  if (!hint) return;
  try {
    if (tab?.url && (tab.url.startsWith('http://') || tab.url.startsWith('https://'))) {
      const host = new URL(tab.url).hostname;
      hint.innerHTML = `Sẽ xóa dữ liệu của <strong>${host}</strong> (cookies, cache, storage…) rồi reload.`;
      hint.className = 'hint ok';
    } else {
      hint.textContent = 'Mở tab http/https rồi bấm BẮT ĐẦU.';
      hint.className = 'hint err';
    }
  } catch {
    hint.textContent = 'Mở tab http/https rồi bấm BẮT ĐẦU.';
    hint.className = 'hint err';
  }
}

function parseClearSec(val) {
  const n = parseInt(String(val).trim(), 10);
  if (!Number.isFinite(n)) return selectedClearSec;
  return Math.max(1, Math.min(3600, n));
}

document.querySelectorAll('.clear-preset-btn').forEach((btn) => {
  btn.addEventListener('click', async () => {
    const sec = parseInt(btn.dataset.sec, 10);
    highlightClearPreset(sec);
    await send('SET_CLEAR_INTERVAL', { intervalSec: sec });
    await refreshClearState();
  });
});

$('clear-custom-input').addEventListener('change', async () => {
  const sec = parseClearSec($('clear-custom-input').value);
  highlightClearPreset(sec);
  await send('SET_CLEAR_INTERVAL', { intervalSec: sec });
  await refreshClearState();
});

$('clear-custom-input').addEventListener('blur', async () => {
  const sec = parseClearSec($('clear-custom-input').value);
  highlightClearPreset(sec);
  await send('SET_CLEAR_INTERVAL', { intervalSec: sec });
  await refreshClearState();
});

$('btn-clear-start').addEventListener('click', async () => {
  const startBtn = $('btn-clear-start');
  if (startBtn.dataset.busy === '1') return;

  const sec = parseClearSec($('clear-custom-input').value);
  const tab = await getTargetTab();

  startBtn.dataset.busy = '1';
  startBtn.textContent = 'Đang bật…';

  let started = false;
  try {
    const res = await send('START_AUTO_CLEAR', { intervalSec: sec, tabId: tab?.id }, 15000);
    if (!res) {
      alert('Extension không phản hồi.\nReload extension tại chrome://extensions/');
      return;
    }
    if (!res.ok) {
      const urlHint = tab?.url ? `\n\nTab: ${tab.url}` : '';
      const msg = res.message ? `\n\n${res.message}` : '';
      alert(`Không bắt đầu được.\nMở tab http/https và cấp quyền browsingData.${urlHint}${msg}`);
      return;
    }
    started = true;
    await refreshClearState();
    startClearCountdownTicker();
  } finally {
    if (!started) resetClearStartButton(false);
    else {
      const cs = await send('GET_CLEAR_STATE', {}, 5000);
      if (cs) renderClear(cs);
      else resetClearStartButton(true);
    }
  }
});

$('btn-clear-stop').addEventListener('click', async () => {
  await send('STOP_AUTO_CLEAR');
  await refreshClearState();
});

$('btn-clear-reset').addEventListener('click', async () => {
  await send('RESET_CLEAR_COUNT');
  await refreshClearState();
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === 'STATUS_PUSH' || message.type === 'REQUEST_LOG_UPDATE') fetchStatus();
  if (message.type === 'CLEAR_STATE_UPDATE') renderClear(message.state);
});

chrome.storage.local.get('f2apiActiveTab').then((data) => {
  if (data.f2apiActiveTab === 'clear') switchTab('clear');
});

fetchStatus();
setInterval(fetchStatus, 1500);
resetClearStartButton(false);
refreshClearState();
