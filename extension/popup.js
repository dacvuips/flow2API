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
  $('tabMode').classList.toggle('active', name === 'mode');
  $('tabMode').hidden = name !== 'mode';
  if ($('tabChatgpt')) {
    $('tabChatgpt').classList.toggle('active', name === 'chatgpt');
    $('tabChatgpt').hidden = name !== 'chatgpt';
  }

  if (name === 'mode') {
    refreshModePanel();
    startModeStatsTicker();
  } else {
    stopModeStatsTicker();
  }

  if (name === 'chatgpt') {
    refreshChatgptCookies();
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
  if (data.f2apiActiveTab === 'mode' || data.f2apiActiveTab === 'chatgpt') {
    switchTab(data.f2apiActiveTab);
  }
});

fetchStatus();
setInterval(fetchStatus, 1500);
refreshModePanel();

// ── ChatGPT cookies (read-only) ───────────────────────────────────────

async function refreshChatgptCookies() {
  const hint = $('cgpt-hint');
  setText('cgpt-login', 'đang tải…');
  const status = await send('CHATGPT_COOKIE_STATUS');
  if (!status || status.ok === false) {
    setText('cgpt-login', 'lỗi');
    setText('cgpt-account', '—');
    setText('cgpt-did', '—');
    setText('cgpt-count', '0');
    if ($('cgpt-flags')) $('cgpt-flags').innerHTML = '';
    if ($('cgpt-list')) $('cgpt-list').innerHTML = '';
    if (hint) {
      hint.textContent = status?.error
        ? `Lỗi: ${status.error}`
        : 'Không đọc được cookie. Reload extension và thử lại.';
      hint.className = 'hint err';
    }
    return;
  }

  setText('cgpt-login', status.loggedIn ? 'đã đăng nhập' : 'chưa đăng nhập');
  const loginEl = $('cgpt-login');
  if (loginEl) {
    loginEl.className = status.loggedIn ? 'value token-ready' : 'value token-missing';
  }
  setText('cgpt-account', status.email || status.name || '—');
  setText('cgpt-did', status.deviceId ? `${String(status.deviceId).slice(0, 8)}…` : '—');
  const did = $('cgpt-did');
  if (did) did.title = status.deviceId || '';
  setText('cgpt-count', status.cookieCount || 0);

  const flags = $('cgpt-flags');
  if (flags) {
    const present = status.present || {};
    flags.innerHTML = Object.keys(present).map((name) => {
      const on = !!present[name];
      const short = name.replace('__Secure-next-auth.session-token', 'session-token');
      return `<span class="cgpt-flag ${on ? 'on' : 'off'}" title="${name}">${short}: ${on ? 'OK' : 'no'}</span>`;
    }).join('');
  }

  const list = $('cgpt-list');
  if (list) {
    const rows = status.cookies || [];
    if (!rows.length) {
      list.innerHTML = '<div class="cgpt-row"><div class="cgpt-name">Không có cookie chatgpt.com</div></div>';
    } else {
      list.innerHTML = rows.map((c) => `
        <div class="cgpt-row">
          <div>
            <div class="cgpt-name">${c.name}</div>
            <div class="cgpt-meta">${c.domain}${c.path || ''} · ${c.httpOnly ? 'HttpOnly' : 'JS'} · ${c.secure ? 'Secure' : 'Insecure'}</div>
          </div>
          <div class="cgpt-val" title="${c.valuePreview || ''}">${c.valuePreview || ''}</div>
        </div>
      `).join('');
    }
  }

  if (hint) {
    hint.textContent = status.loggedIn
      ? 'Cookie OK (ẩn giá trị). Có thể gửi conversation bên dưới.'
      : 'Chưa thấy session. Bấm “Mở ChatGPT”, đăng nhập, rồi Làm mới.';
    hint.className = status.loggedIn ? 'hint ok' : 'hint';
  }
}

$('btn-cgpt-refresh')?.addEventListener('click', () => refreshChatgptCookies());
$('btn-cgpt-open')?.addEventListener('click', async () => {
  const r = await send('CHATGPT_OPEN_TAB');
  if (!r?.ok) {
    setHint('cgpt-hint', `Không mở được tab: ${r?.error || 'unknown'}`, 'err');
  }
});

$('btn-cgpt-send')?.addEventListener('click', async () => {
  const prompt = ($('cgpt-prompt')?.value || '').trim();
  const endpoint = ($('cgpt-endpoint')?.value || '').trim();
  const replyEl = $('cgpt-reply');
  const btn = $('btn-cgpt-send');
  if (!prompt) {
    setHint('cgpt-chat-hint', 'Nhập prompt trước khi gửi.', 'err');
    return;
  }
  if (btn) btn.disabled = true;
  setHint('cgpt-chat-hint', 'Đang gọi conversation…', '');
  if (replyEl) {
    replyEl.classList.add('on');
    replyEl.textContent = '…';
  }
  try {
    const r = await send('CHATGPT_SEND', { prompt, endpoint: endpoint || undefined }, 120000);
    if (!r?.ok) {
      const err = r?.error || 'send_failed';
      setHint('cgpt-chat-hint', `Lỗi: ${err}`, 'err');
      if (replyEl) replyEl.textContent = String(err);
      return;
    }
    setHint('cgpt-chat-hint', `OK · ${r.endpoint || endpoint || 'conversation'}`, 'ok');
    if (replyEl) replyEl.textContent = r.text || '(empty response)';
  } catch (e) {
    setHint('cgpt-chat-hint', `Lỗi: ${e?.message || e}`, 'err');
    if (replyEl) replyEl.textContent = String(e?.message || e);
  } finally {
    if (btn) btn.disabled = false;
  }
});
