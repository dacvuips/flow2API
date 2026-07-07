/** Flow2API — Popup UI */

const $ = (id) => document.getElementById(id);

let selectedClearSec = 5;
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
  $('tabMode').classList.toggle('active', name === 'mode');
  $('tabMode').hidden = name !== 'mode';

  if (name === 'clear') {
    refreshClearState();
    startClearCountdownTicker();
  } else {
    stopClearCountdownTicker();
  }
  if (name === 'mode') {
    refreshModePanel();
    startModeStatsTicker();
  } else {
    stopModeStatsTicker();
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
  setText('clear-interval-label', formatIntervalLabel(cs.intervalSec ?? 5));

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
    if (tab?.url && isFlowProjectUrl(tab.url)) {
      hint.innerHTML = `Chỉ xóa <strong>Cookies</strong> của <strong>labs.google</strong> mỗi <strong>${selectedClearSec}s</strong> (chỉ khi tab đang ở trang project).`;
      hint.className = 'hint ok';
    } else if (tab?.url && isLabsGoogleUrl(tab.url)) {
      hint.innerHTML = 'Tab Flow chưa vào project — mở URL có <strong>/project/</strong> (vd. <code>…/flow/project/…</code>) để auto clear chạy.';
      hint.className = 'hint';
    } else if (tab?.url && (tab.url.startsWith('http://') || tab.url.startsWith('https://'))) {
      const host = new URL(tab.url).hostname;
      hint.innerHTML = `Tab hiện tại (<strong>${host}</strong>) không phải Flow project — mở project trên <strong>labs.google</strong>.`;
      hint.className = 'hint';
    } else {
      hint.textContent = 'Mở tab Flow project (đường dẫn có /project/) trên labs.google.';
      hint.className = 'hint err';
    }
  } catch {
    hint.textContent = 'Mở tab Flow project (đường dẫn có /project/) trên labs.google.';
    hint.className = 'hint err';
  }
}

function isFlowProjectUrl(url) {
  try {
    const u = new URL(String(url || ''));
    if (u.hostname !== 'labs.google') return false;
    return u.pathname.includes('/project/');
  } catch {
    return false;
  }
}

function isLabsGoogleUrl(url) {
  try {
    return new URL(String(url || '')).hostname === 'labs.google';
  } catch {
    return false;
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
      alert(`Không bắt đầu được.\nCần tab Flow project (đường dẫn có /project/).${urlHint}${msg}`);
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

// Active tab restore is consolidated at the bottom of the file (handles mode|clear).

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

// ── Mode toggle ────────────────────────────────────────────────────────

let modeStatsTimer = null;
let currentMode = 'bridge';
let selectedMode = 'bridge';

function setModeHero(mode) {
  const badge = $('mode-badge');
  const sub = $('mode-sub');
  const hero = $('mode-hero');
  const isCenter = mode === 'center';
  if (badge) {
    badge.textContent = isCenter ? 'Captcha Center' : 'Bridge';
    badge.className = `badge ${isCenter ? 'center' : 'bridge'}`;
  }
  if (sub) {
    sub.textContent = isCenter
      ? 'Chuyên mint reCAPTCHA — long-poll agent, phục vụ Bridge profiles khác.'
      : 'Proxy API request từ agent, inject captcha do Center cấp (không tự solve).';
  }
  if (hero) hero.classList.toggle('center', isCenter);
}

async function refreshModePanel() {
  const data = await chrome.storage.local.get([
    'f2apiExtMode',
    'centerBridgeBase',
    'centerBridgeSecret',
    'centerLabel',
  ]);
  currentMode = data.f2apiExtMode === 'center' ? 'center' : 'bridge';
  selectedMode = currentMode;
  setModeHero(currentMode);

  const rBridge = $('mode-bridge');
  const rCenter = $('mode-center');
  if (rBridge) rBridge.checked = currentMode === 'bridge';
  if (rCenter) rCenter.checked = currentMode === 'center';

  const centerCfg = $('center-config');
  if (centerCfg) centerCfg.hidden = selectedMode !== 'center';

  $('center-base').value = String(data.centerBridgeBase || 'http://127.0.0.1:1994');
  $('center-secret').value = String(data.centerBridgeSecret || '');
  $('center-label').value = String(data.centerLabel || '');

  await refreshCenterStats();
}

async function refreshCenterStats() {
  const sess = await chrome.storage.session.get([
    'centerLastMintOkAt',
    'centerLastMintDurationMs',
    'centerLastPollOkAt',
  ]);
  const setN = (id, v) => setText(id, v);
  setN('cs-mint', 'session');

  const fmt = (ts) => {
    if (!ts) return '—';
    const s = Math.round((Date.now() - ts) / 1000);
    if (s < 5) return 'vừa xong';
    if (s < 60) return `${s}s`;
    return `${Math.floor(s / 60)}m ${s % 60}s`;
  };
  setN('cs-last', fmt(sess.centerLastMintOkAt));
  setN('cs-poll', fmt(sess.centerLastPollOkAt));

  const base = ($('center-base')?.value || 'http://127.0.0.1:1994').replace(/\/+$/, '');
  try {
    const resp = await fetch(`${base}/api/internal/captcha/stats`);
    if (resp.ok) {
      const d = await resp.json();
      setN('cs-online', `${d?.online_count ?? 0}`);
      setN('cs-mint', String(d?.centers?.reduce((s, c) => s + (c?.mint_count || 0), 0) ?? 0));
    } else {
      setN('cs-online', '—');
    }
  } catch {
    setN('cs-online', 'offline');
  }
}

function startModeStatsTicker() {
  stopModeStatsTicker();
  modeStatsTimer = setInterval(refreshCenterStats, 3000);
}

function stopModeStatsTicker() {
  if (modeStatsTimer) {
    clearInterval(modeStatsTimer);
    modeStatsTimer = null;
  }
}

document.querySelectorAll('input[name="mode"]').forEach((r) => {
  r.addEventListener('change', () => {
    selectedMode = r.value === 'center' ? 'center' : 'bridge';
    $('center-config').hidden = selectedMode !== 'center';
  });
});

$('btn-mode-cancel')?.addEventListener('click', () => refreshModePanel());

$('btn-mode-apply')?.addEventListener('click', async () => {
  if (selectedMode === currentMode) {
    setHint('center-hint', 'Không có thay đổi.', 'ok');
    return;
  }
  await chrome.storage.local.set({ f2apiExtMode: selectedMode });
  // Save current Center config nếu đang chuyển vào center
  if (selectedMode === 'center') {
    await saveCenterConfig(false);
  }
  chrome.runtime.reload();
});

$('btn-center-save')?.addEventListener('click', async () => {
  await saveCenterConfig(true);
});

$('btn-center-test')?.addEventListener('click', async () => {
  const base = $('center-base').value.trim().replace(/\/+$/, '');
  const secret = $('center-secret').value.trim();
  if (!base) { setHint('center-hint', 'Nhập Bridge URL', 'err'); return; }
  try {
    // Nếu chưa có secret, thử fetch từ loopback trước
    let useSecret = secret;
    if (!useSecret) {
      const r = await fetch(`${base}/api/internal/captcha/secret`);
      if (r.ok) {
        const d = await r.json();
        if (d?.secret) {
          useSecret = String(d.secret);
          $('center-secret').value = useSecret;
        }
      }
    }
    if (!useSecret) {
      setHint('center-hint', 'Chưa có secret — chạy agent trước hoặc paste secret.', 'err');
      return;
    }
    const resp = await fetch(`${base}/api/internal/captcha/stats`);
    if (resp.ok) {
      const d = await resp.json();
      setHint(
        'center-hint',
        `✓ OK — online=${d.online_count}, queued=${d.queued_count}, pending=${d.pending_count}`,
        'ok',
      );
    } else {
      setHint('center-hint', `HTTP ${resp.status}`, 'err');
    }
  } catch (e) {
    setHint('center-hint', `Lỗi: ${e?.message || e}`, 'err');
  }
});

async function saveCenterConfig(showHint) {
  const centerBridgeBase = $('center-base').value.trim().replace(/\/+$/, '');
  const centerBridgeSecret = $('center-secret').value.trim();
  const centerLabel = $('center-label').value.trim();
  await chrome.storage.local.set({ centerBridgeBase, centerBridgeSecret, centerLabel });
  if (showHint) setHint('center-hint', '✓ Đã lưu cấu hình', 'ok');
}

function setHint(id, msg, cls) {
  const el = $(id);
  if (!el) return;
  el.textContent = msg;
  el.className = `hint ${cls || ''}`.trim();
}

// Restore active tab
chrome.storage.local.get('f2apiActiveTab').then((data) => {
  if (data.f2apiActiveTab === 'mode') switchTab('mode');
  else if (data.f2apiActiveTab === 'clear') switchTab('clear');
});

fetchStatus();
loadAutoClickSetting();
setInterval(fetchStatus, 1500);
resetClearStartButton(false);
refreshClearState();
refreshModePanel();
