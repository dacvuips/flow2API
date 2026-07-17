/** Flow2API — Popup UI */

const $ = (id) => document.getElementById(id);

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

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach((btn) => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('tabStatus').classList.toggle('active', name === 'status');
  $('tabStatus').hidden = name !== 'status';
  $('tabMode').classList.toggle('active', name === 'mode');
  $('tabMode').hidden = name !== 'mode';

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
  if (data.f2apiActiveTab === 'mode') {
    switchTab('mode');
  } else if (data.f2apiActiveTab === 'chatgpt') {
    // Tab Chat GPT đã gỡ — về Trạng thái
    chrome.storage.local.set({ f2apiActiveTab: 'status' }).catch(() => {});
  }
});

fetchStatus();
setInterval(fetchStatus, 1500);
refreshModePanel();
