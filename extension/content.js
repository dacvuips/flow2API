/**
 * Content script — bridge between background.js and injected.js.
 * Injects injected.js into MAIN world, forwards GET_CAPTCHA messages,
 * and renders the Flow2API status popup inside Google Flow pages.
 */
(function injectCaptchaBridge() {
  if (document.documentElement?.dataset?.flow2apiInjected === '1') return;
  document.documentElement.dataset.flow2apiInjected = '1';
  const s = document.createElement('script');
  s.src = chrome.runtime.getURL('injected.js');
  s.onload = () => s.remove();
  (document.head || document.documentElement).appendChild(s);
})();

const STATUS_LABELS = {
  idle: 'Đã kết nối',
  running: 'Đang xử lý',
  off: 'Mất kết nối',
};

const STATUS_ICONS = {
  idle: '●',
  running: '▶',
  off: '○',
};

const STATUS_COLORS = {
  idle: '#22c55e',
  running: '#f5b301',
  off: '#6b7280',
};

let statusRoot = null;
let statusText = null;
let statusDot = null;
let statusTimer = null;
let autoClickFeedbackUntil = 0;
let autoClickPolling = false;

const AUTO_CLICK_POLL_MS = 5000;

function ensureStatusPopup() {
  if (statusRoot) return;
  statusRoot = document.createElement('div');
  statusRoot.id = 'flow2api-page-status';
  statusRoot.setAttribute('role', 'status');
  statusRoot.setAttribute('aria-live', 'polite');
  statusRoot.innerHTML = `
    <div class="flow2api-page-status__brand"><span>Flow</span><b>2API</b></div>
    <div class="flow2api-page-status__state"><span class="flow2api-page-status__dot">○</span><span class="flow2api-page-status__text">Đang kiểm tra</span></div>
  `;
  const style = document.createElement('style');
  style.textContent = `
    #flow2api-page-status {
      position: fixed;
      left: 50%;
      top: 18px;
      transform: translateX(-50%);
      z-index: 2147483647;
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 168px;
      padding: 10px 12px;
      border: 1px solid rgba(226, 232, 240, 0.92);
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.94);
      color: #0f172a;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.16);
      backdrop-filter: blur(10px);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 12px;
      line-height: 1;
      user-select: none;
      pointer-events: none;
    }
    #flow2api-page-status .flow2api-page-status__brand {
      font-weight: 780;
      letter-spacing: -0.03em;
      white-space: nowrap;
    }
    #flow2api-page-status .flow2api-page-status__brand span { color: #111827; }
    #flow2api-page-status .flow2api-page-status__brand b { color: #1d8cff; margin-left: 1px; font-weight: 850; }
    #flow2api-page-status .flow2api-page-status__state {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding-left: 10px;
      border-left: 1px solid #e2e8f0;
      color: #334155;
      white-space: nowrap;
      font-weight: 650;
    }
    #flow2api-page-status .flow2api-page-status__dot {
      color: #6b7280;
      font-size: 13px;
      line-height: 1;
    }
    @media (prefers-reduced-motion: reduce) {
      #flow2api-page-status { transition: none !important; }
    }
  `;
  document.documentElement.appendChild(style);
  document.documentElement.appendChild(statusRoot);
  statusDot = statusRoot.querySelector('.flow2api-page-status__dot');
  statusText = statusRoot.querySelector('.flow2api-page-status__text');
}

function renderStatus(status) {
  if (Date.now() < autoClickFeedbackUntil || autoClickPolling) return;
  ensureStatusPopup();
  const current = status?.manualDisconnect || !status?.connected ? 'off' : (status.state || 'idle');
  const state = current === 'running' ? 'running' : (current === 'idle' ? 'idle' : 'off');
  statusDot.textContent = STATUS_ICONS[state];
  statusDot.style.color = STATUS_COLORS[state];
  statusText.textContent = STATUS_LABELS[state];
}

function showAutoClickFeedback(kind) {
  ensureStatusPopup();
  const configs = {
    searching: { icon: '…', color: '#f5b301', text: 'Đang tìm nút Create Flow…' },
    success: { icon: '✓', color: '#22c55e', text: 'Đã click Create Flow' },
  };
  const cfg = configs[kind];
  if (!cfg) return;
  statusDot.textContent = cfg.icon;
  statusDot.style.color = cfg.color;
  statusText.textContent = cfg.text;
  autoClickFeedbackUntil = kind === 'success' ? Date.now() + 10000 : 0;
  chrome.storage.local.set({
    autoClickLastStatus: { status: kind, message: cfg.text, at: Date.now() },
  });
  chrome.runtime.sendMessage({
    type: 'AUTO_CLICK_STATUS',
    status: kind,
    message: cfg.text,
  }).catch(() => {});
}

function refreshStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS' }, (reply) => {
    if (chrome.runtime.lastError) {
      renderStatus({ connected: false, state: 'off' });
      return;
    }
    renderStatus(reply);
  });
}

function startStatusPopup() {
  ensureStatusPopup();
  refreshStatus();
  if (!statusTimer) statusTimer = setInterval(refreshStatus, 1500);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startStatusPopup, { once: true });
} else {
  startStatusPopup();
}

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type === 'STATUS_PUSH' || msg.type === 'REQUEST_LOG_UPDATE') {
    refreshStatus();
    return false;
  }

  if (msg.type === 'RETRY_AUTO_CLICK_CREATE') {
    autoClickDoneForPath = '';
    stopAutoClickCreateFlow();
    startAutoClickCreateFlow();
    reply?.({ ok: true });
    return false;
  }

  if (msg.type !== 'GET_CAPTCHA') return false;

  const { requestId, pageAction } = msg;

  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      document.removeEventListener('CAPTCHA_RESULT', handler);
      clearTimeout(timer);
      reply({ token: e.detail.token, error: e.detail.error });
    }
  };

  const timer = setTimeout(() => {
    document.removeEventListener('CAPTCHA_RESULT', handler);
    reply({ error: 'CONTENT_TIMEOUT' });
  }, 25000);

  document.addEventListener('CAPTCHA_RESULT', handler);

  document.dispatchEvent(new CustomEvent('GET_CAPTCHA', {
    detail: { requestId, pageAction },
  }));

  return true; // keep channel open for async reply
});

// ─── Auto-click "Create with Google Flow" on landing page ───────────────────

let autoClickTimer = null;
let autoClickDoneForPath = '';
let autoClickEnabled = true;
let mainWorldReady = false;

function isFlowPage() {
  try {
    const path = window.location.pathname || '';
    return /\/fx(\/|$)/i.test(path) || /\/tools\/flow/i.test(path);
  } catch {
    return true;
  }
}

function currentPageKey() {
  return `${window.location.pathname}${window.location.search}`;
}

function waitForMainWorldReady(maxMs = 15000) {
  return new Promise((resolve) => {
    const start = Date.now();
    const check = () => {
      if (document.documentElement?.dataset?.flow2apiMainReady === '1') {
        resolve(true);
        return;
      }
      if (Date.now() - start > maxMs) {
        resolve(false);
        return;
      }
      setTimeout(check, 50);
    };
    check();
  });
}

function requestMainWorldAutoClick() {
  const requestId = `ac_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
  return new Promise((resolve) => {
    const handler = (e) => {
      if (e.detail?.requestId !== requestId) return;
      document.removeEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
      clearTimeout(timer);
      resolve(e.detail || { clicked: false, reason: 'empty_result' });
    };
    const timer = setTimeout(() => {
      document.removeEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
      resolve({ clicked: false, reason: 'main_world_timeout' });
    }, 3000);
    document.addEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
    document.dispatchEvent(new CustomEvent('FLOW2API_TRY_AUTO_CLICK_CREATE', {
      detail: { requestId },
    }));
  });
}

function stopAutoClickCreateFlow() {
  autoClickPolling = false;
  if (autoClickTimer) {
    clearInterval(autoClickTimer);
    autoClickTimer = null;
  }
}

async function tickAutoClickCreateFlow() {
  if (!autoClickEnabled || !isFlowPage()) {
    stopAutoClickCreateFlow();
    refreshStatus();
    return;
  }

  const pathKey = currentPageKey();
  if (autoClickDoneForPath === pathKey) {
    stopAutoClickCreateFlow();
    return;
  }

  if (!mainWorldReady) {
    mainWorldReady = await waitForMainWorldReady();
    if (!mainWorldReady) return;
  }

  const result = await requestMainWorldAutoClick();
  if (result.clicked) {
    autoClickDoneForPath = pathKey;
    showAutoClickFeedback('success');
    stopAutoClickCreateFlow();
  }
}

function startAutoClickCreateFlow() {
  if (!autoClickEnabled || !isFlowPage()) return;
  if (autoClickTimer) return;

  autoClickPolling = true;
  showAutoClickFeedback('searching');
  tickAutoClickCreateFlow();
  autoClickTimer = setInterval(tickAutoClickCreateFlow, AUTO_CLICK_POLL_MS);
}

function bootAutoClickCreateFlow() {
  if (!autoClickEnabled) return;
  let watchedPathKey = currentPageKey();
  const start = () => startAutoClickCreateFlow();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
  setInterval(() => {
    const key = currentPageKey();
    if (key === watchedPathKey) return;
    watchedPathKey = key;
    autoClickDoneForPath = '';
    stopAutoClickCreateFlow();
    startAutoClickCreateFlow();
  }, 1000);
  window.addEventListener('pageshow', () => {
    autoClickDoneForPath = '';
    mainWorldReady = document.documentElement?.dataset?.flow2apiMainReady === '1';
    stopAutoClickCreateFlow();
    startAutoClickCreateFlow();
  });
}

function initAutoClickCreateFlow() {
  chrome.storage.local.get(['autoClickCreateFlow'], (data) => {
    if (chrome.runtime.lastError) return;
    autoClickEnabled = data.autoClickCreateFlow !== false;
    if (!autoClickEnabled) return;
    bootAutoClickCreateFlow();
  });
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.autoClickCreateFlow) return;
  autoClickEnabled = changes.autoClickCreateFlow.newValue !== false;
  if (!autoClickEnabled) {
    stopAutoClickCreateFlow();
    return;
  }
  autoClickDoneForPath = '';
  stopAutoClickCreateFlow();
  bootAutoClickCreateFlow();
});

initAutoClickCreateFlow();
