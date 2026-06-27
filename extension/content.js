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
  ensureStatusPopup();
  const current = status?.manualDisconnect || !status?.connected ? 'off' : (status.state || 'idle');
  const state = current === 'running' ? 'running' : (current === 'idle' ? 'idle' : 'off');
  statusDot.textContent = STATUS_ICONS[state];
  statusDot.style.color = STATUS_COLORS[state];
  statusText.textContent = STATUS_LABELS[state];
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

  if (msg.type !== 'GET_CAPTCHA') return false;

  const { requestId, pageAction } = msg;

  const handler = (e) => {
    if (e.detail?.requestId === requestId) {
      window.removeEventListener('CAPTCHA_RESULT', handler);
      clearTimeout(timer);
      reply({ token: e.detail.token, error: e.detail.error });
    }
  };

  const timer = setTimeout(() => {
    window.removeEventListener('CAPTCHA_RESULT', handler);
    reply({ error: 'CONTENT_TIMEOUT' });
  }, 25000);

  window.addEventListener('CAPTCHA_RESULT', handler);

  window.dispatchEvent(new CustomEvent('GET_CAPTCHA', {
    detail: { requestId, pageAction },
  }));

  return true; // keep channel open for async reply
});

// ─── Auto-click "Create with Google Flow" on landing page ───────────────────

const AUTO_CLICK_MAX_MS = 90000;
const AUTO_CLICK_POLL_MS = 400;

let autoClickObserver = null;
let autoClickTimer = null;
let autoClickStartedAt = 0;
let autoClickDoneForPath = '';
let autoClickEnabled = true;

function isFlowLandingPage() {
  try {
    const path = window.location.pathname || '';
    if (/\/project\/[0-9a-f-]{36}/i.test(path)) return false;
    return /\/fx(\/|$)/i.test(path) || /\/tools\/flow/i.test(path);
  } catch {
    return true;
  }
}

function currentLandingPathKey() {
  return `${window.location.pathname}${window.location.search}`;
}

function requestMainWorldAutoClick() {
  const requestId = `ac_${Date.now()}_${Math.random().toString(16).slice(2, 8)}`;
  return new Promise((resolve) => {
    const handler = (e) => {
      if (e.detail?.requestId !== requestId) return;
      window.removeEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
      clearTimeout(timer);
      resolve(e.detail || { clicked: false, reason: 'empty_result' });
    };
    const timer = setTimeout(() => {
      window.removeEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
      resolve({ clicked: false, reason: 'main_world_timeout' });
    }, 1200);
    window.addEventListener('FLOW2API_AUTO_CLICK_RESULT', handler);
    window.dispatchEvent(new CustomEvent('FLOW2API_TRY_AUTO_CLICK_CREATE', {
      detail: { requestId },
    }));
  });
}

function stopAutoClickCreateFlow() {
  if (autoClickObserver) {
    autoClickObserver.disconnect();
    autoClickObserver = null;
  }
  if (autoClickTimer) {
    clearInterval(autoClickTimer);
    autoClickTimer = null;
  }
}

async function tryAutoClickCreateFlow() {
  if (!autoClickEnabled) {
    stopAutoClickCreateFlow();
    return true;
  }
  if (!isFlowLandingPage()) {
    stopAutoClickCreateFlow();
    return true;
  }

  const pathKey = currentLandingPathKey();
  if (autoClickDoneForPath === pathKey) {
    stopAutoClickCreateFlow();
    return true;
  }

  const result = await requestMainWorldAutoClick();
  if (result.clicked) {
    autoClickDoneForPath = pathKey;
    console.log('[Flow2API] Auto-clicked "Create with Google Flow"', result);
    stopAutoClickCreateFlow();
    return true;
  }
  return false;
}

function startAutoClickCreateFlow() {
  if (!autoClickEnabled || !isFlowLandingPage()) return;
  stopAutoClickCreateFlow();
  autoClickStartedAt = Date.now();

  const tick = () => {
    tryAutoClickCreateFlow().then((done) => {
      if (done) return;
      if (Date.now() - autoClickStartedAt > AUTO_CLICK_MAX_MS) {
        console.warn('[Flow2API] Auto-click gave up after timeout');
        stopAutoClickCreateFlow();
      }
    });
  };

  tick();

  autoClickObserver = new MutationObserver(tick);
  autoClickObserver.observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  autoClickTimer = setInterval(tick, AUTO_CLICK_POLL_MS);
}

function bootAutoClickCreateFlow() {
  if (!autoClickEnabled) return;
  // SPA / React: wait for body and late-rendered hero button.
  const start = () => {
    startAutoClickCreateFlow();
    setTimeout(startAutoClickCreateFlow, 1500);
    setTimeout(startAutoClickCreateFlow, 4000);
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
  window.addEventListener('load', start, { once: true });
  window.addEventListener('pageshow', () => {
    autoClickDoneForPath = '';
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
  bootAutoClickCreateFlow();
});

initAutoClickCreateFlow();
