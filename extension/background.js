/**
 * Flow2API Bridge Ă¢â‚¬â€ Chrome Extension Background Service Worker
 *
 * Connects to local Python agent via WebSocket (agent runs WS server).
 * Captures Bearer token and proxies API calls through the browser context.
 *
 * Modes (chọn ở popup):
 *  - 'bridge' (default): WS bridge — proxy api_request, inject captchaToken
 *     (do agent broker cấp từ Captcha Center). Không tự solve trên tab worker.
 *  - 'center':  Long-poll agent's captcha broker, solve grecaptcha, phục vụ
 *     token cho các Bridge profile khác. Không WS, không proxy Bridge.
 * Mode đọc từ chrome.storage.local key 'f2apiExtMode'.
 */

// Nạp Captcha Center loop (chỉ chạy khi mode = 'center' — được gọi trong init()).
try {
  importScripts('center-loop.js');
} catch (e) {
  console.error('[Flow2API] importScripts(center-loop.js) failed:', e);
}

const AGENT_WS_URL  = 'ws://127.0.0.1:1609';
const CALLBACK_URL  = 'http://127.0.0.1:1994/api/ext/callback';
const TOKEN_SOFT_MAX_AGE_MS = 5 * 60 * 1000;
const TOKEN_REFRESH_WAIT_MS = 10000;
const FLOW_WATCHDOG_STALE_MS = 20 * 60 * 1000;
const FLOW_WATCHDOG_RELOAD_COOLDOWN_MS = 10 * 60 * 1000;

let ws               = null;
let flowKey          = null;
let profileId        = null; // Unique per Chrome profile (chrome.storage.local)
let callbackSecret   = null; // Auth secret received from agent on WS connect
let state            = 'off'; // off | idle | running
let manualDisconnect = false;
/** When false, do not auto-open Flow tabs/windows (dashboard "ngừng nhận job"). */
let dispatchEnabled  = true;
let metrics = {
  tokenCapturedAt: null,
  requestCount:    0,
  successCount:    0,
  failedCount:     0,
  lastError:       null,
};

// ─── Cancel / Abort tracking ─────────────────────────────────────────
// The agent can send { method: 'abort_request', params: { targetId } }
// to tell us to stop a specific in-flight api_request / trpc_request /
// raw_request that we already accepted. Fire-and-forget from the agent's
// side — we never send a response for the abort message itself.
//
// abortControllers: keyed by ws message id -> AbortController for fetch()
// abortedRequestIds: keyed by ws message id — set when abort arrived
//   BEFORE we registered a controller (or between checkpoints), so the
//   handler can bail out at the next gate.
const abortControllers = new Map();
const abortedRequestIds = new Set();

function markRequestAborted(targetId) {
  if (!targetId) return;
  const key = String(targetId);
  abortedRequestIds.add(key);
  const controller = abortControllers.get(key);
  if (controller) {
    try { controller.abort(); } catch { /* AbortController.abort never throws in Chrome */ }
  }
}

function isRequestAborted(id) {
  return !!id && abortedRequestIds.has(String(id));
}

function registerAbortController(id, controller) {
  if (!id || !controller) return;
  abortControllers.set(String(id), controller);
}

function clearAbortTracking(id) {
  if (!id) return;
  const key = String(id);
  abortControllers.delete(key);
  abortedRequestIds.delete(key);
}

const flowUrls = ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*'];

const CAPTCHA_RELOAD_EVERY_N_SOLVES = 0;
const _captchaSolvesByTab = new Map();
let _captchaChain = Promise.resolve();

// Cached Flow projectId (in-memory) — informational / popup only.
let _cachedProjectId = null;

// Headers sniffed from real Flow page → aisandbox requests (per Chrome profile).
const FLOW_DNR_HEADERS_RULE_ID = 9001;
const FLOW_POST_CONTENT_TYPE = 'text/plain;charset=UTF-8';
const FLOW_DEFAULT_REFERER = 'https://labs.google/';
const FLOW_DNR_HEADER_KEYS = [
  ['user-agent', 'User-Agent'],
  ['sec-ch-ua', 'sec-ch-ua'],
  ['sec-ch-ua-mobile', 'sec-ch-ua-mobile'],
  ['sec-ch-ua-platform', 'sec-ch-ua-platform'],
  ['x-browser-channel', 'x-browser-channel'],
  ['x-browser-copyright', 'x-browser-copyright'],
  ['x-browser-validation', 'x-browser-validation'],
  ['x-browser-year', 'x-browser-year'],
  ['x-client-data', 'x-client-data'],
];
const FLOW_HEADER_SKIP = new Set([
  'authorization',
  'cookie',
  'content-length',
  'host',
  'connection',
  'transfer-encoding',
  'upgrade-insecure-requests',
]);
const FLOW_API_HEADER_ALLOWLIST = new Set([
  'accept',
  'accept-language',
  'user-agent',
  'sec-ch-ua',
  'sec-ch-ua-mobile',
  'sec-ch-ua-platform',
  'sec-ch-ua-full-version-list',
  'sec-ch-ua-arch',
  'sec-ch-ua-bitness',
  'sec-ch-ua-model',
  'sec-ch-ua-platform-version',
  'sec-fetch-site',
  'sec-fetch-mode',
  'sec-fetch-dest',
  'sec-fetch-user',
  'priority',
  'x-browser-channel',
  'x-browser-copyright',
  'x-browser-validation',
  'x-browser-year',
  'x-client-data',
  'x-goog-api-client',
]);
let _pageApiHeadersByTab = new Map();
let _pageApiHeadersLatest = {};
let _pageApiHeadersCapturedAt = 0;
const FLOW_HEADERS_PROBE_WAIT_MS = 4000;
const FLOW_HEADERS_MAX_AGE_MS = 30 * 60 * 1000;

function headersArrayToMap(requestHeaders) {
  const out = {};
  for (const h of requestHeaders || []) {
    const name = String(h?.name || '').trim();
    if (!name) continue;
    out[name.toLowerCase()] = String(h.value || '');
  }
  return out;
}

function isExtensionInitiatedRequest(details) {
  return String(details?.initiator || '').startsWith('chrome-extension://');
}

function isFlowSandboxPageRequest(details) {
  if (!String(details?.url || '').includes('aisandbox-pa.googleapis.com')) return false;
  if (String(details?.method || '').toUpperCase() === 'OPTIONS') return false;
  if (isExtensionInitiatedRequest(details)) return false;
  if ((details.tabId ?? -1) < 0) return false;
  const initiator = String(details.initiator || '');
  if (initiator.includes('labs.google')) return true;
  return details.type === 'xmlhttprequest' || details.type === 'other';
}

function mergeSniffedHeadersInto(target, mapped, method) {
  let changed = false;
  for (const [lower, value] of Object.entries(mapped)) {
    if (!value || FLOW_HEADER_SKIP.has(lower)) continue;
    if (lower === 'content-type') {
      if (method !== 'POST' || !value.includes('text/plain')) continue;
    }
    if (
      lower === 'referer'
      || lower === 'origin'
      || lower === 'content-type'
      || FLOW_API_HEADER_ALLOWLIST.has(lower)
    ) {
      if (target[lower] !== value) {
        target[lower] = value;
        changed = true;
      }
    }
  }
  return changed;
}

function hasUsableApiHeaders(headers) {
  if (!headers || typeof headers !== 'object') return false;
  return !!(headers['user-agent'] || headers['sec-ch-ua'] || headers['x-browser-validation']);
}

function getPageApiHeadersForTab(tabId) {
  if (tabId != null && tabId >= 0) {
    const entry = _pageApiHeadersByTab.get(tabId);
    if (entry?.headers && hasUsableApiHeaders(entry.headers)) {
      return entry.headers;
    }
  }
  return _pageApiHeadersLatest;
}

function noteCapturedPageApiHeaders(tabId, mapped, method) {
  let changed = false;
  if (tabId != null && tabId >= 0) {
    const prev = _pageApiHeadersByTab.get(tabId)?.headers || {};
    const next = { ...prev };
    if (mergeSniffedHeadersInto(next, mapped, method)) {
      _pageApiHeadersByTab.set(tabId, { headers: next, capturedAt: Date.now() });
      changed = true;
    }
  }
  if (mergeSniffedHeadersInto(_pageApiHeadersLatest, mapped, method)) {
    changed = true;
  }
  if (!changed) return false;
  _pageApiHeadersCapturedAt = Date.now();
  const byTabObj = {};
  for (const [id, entry] of _pageApiHeadersByTab.entries()) {
    byTabObj[String(id)] = entry;
  }
  chrome.storage.session.set({
    pageApiHeadersByTab: byTabObj,
    pageApiHeadersLatest: _pageApiHeadersLatest,
    pageApiHeadersCapturedAt: _pageApiHeadersCapturedAt,
  }).catch(() => {});
  syncFlowApiHeaderDnrRules(_pageApiHeadersLatest).catch(() => {});
  return true;
}

function captureFlowApiHeadersFromDetails(details) {
  if (!isFlowSandboxPageRequest(details)) return;
  const mapped = headersArrayToMap(details.requestHeaders);
  const method = String(details.method || '').toUpperCase();
  noteCapturedPageApiHeaders(details.tabId, mapped, method);
}

async function loadCapturedFlowApiHeaders() {
  try {
    const data = await chrome.storage.session.get([
      'pageApiHeadersByTab',
      'pageApiHeadersLatest',
      'pageApiHeadersCapturedAt',
      'pageApiHeaders',
    ]);
    if (data.pageApiHeadersByTab && typeof data.pageApiHeadersByTab === 'object') {
      _pageApiHeadersByTab = new Map();
      for (const [id, entry] of Object.entries(data.pageApiHeadersByTab)) {
        const tabId = Number(id);
        if (!Number.isNaN(tabId) && entry?.headers) {
          _pageApiHeadersByTab.set(tabId, entry);
        }
      }
    }
    if (data.pageApiHeadersLatest && typeof data.pageApiHeadersLatest === 'object') {
      _pageApiHeadersLatest = data.pageApiHeadersLatest;
    } else if (data.pageApiHeaders && typeof data.pageApiHeaders === 'object') {
      _pageApiHeadersLatest = data.pageApiHeaders;
    }
    _pageApiHeadersCapturedAt = Number(data.pageApiHeadersCapturedAt) || 0;
    await syncFlowApiHeaderDnrRules(_pageApiHeadersLatest);
  } catch {
    /* ignore */
  }
}

async function syncFlowApiHeaderDnrRules(headerSource = _pageApiHeadersLatest) {
  const requestHeaders = [];
  for (const [lower, canonical] of FLOW_DNR_HEADER_KEYS) {
    const value = headerSource?.[lower];
    if (value) {
      requestHeaders.push({ header: canonical, operation: 'set', value });
    }
  }
  if (!requestHeaders.length) {
    await chrome.declarativeNetRequest.updateDynamicRules({
      removeRuleIds: [FLOW_DNR_HEADERS_RULE_ID],
    });
    return;
  }
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [FLOW_DNR_HEADERS_RULE_ID],
    addRules: [{
      id: FLOW_DNR_HEADERS_RULE_ID,
      priority: 3,
      action: {
        type: 'modifyHeaders',
        requestHeaders,
      },
      condition: {
        urlFilter: '||aisandbox-pa.googleapis.com^',
        resourceTypes: ['xmlhttprequest'],
        initiatorDomains: [chrome.runtime.id],
      },
    }],
  });
}

async function pickPrimaryFlowTab() {
  const tabs = await chrome.tabs.query({ url: flowUrls });
  return tabs.find((t) => !t.discarded && (t.id ?? -1) >= 0) || tabs[0] || null;
}

/** Sniff aisandbox headers from the same Flow tab used for reCAPTCHA (webRequest). */
async function ensureFlowApiHeadersFromTab(tabId, reason = 'api_request') {
  if (tabId == null || tabId < 0) return false;
  const entry = _pageApiHeadersByTab.get(tabId);
  const age = entry?.capturedAt ? Date.now() - entry.capturedAt : Infinity;
  if (hasUsableApiHeaders(entry?.headers) && age < FLOW_HEADERS_MAX_AGE_MS) {
    await syncFlowApiHeaderDnrRules(entry.headers);
    return true;
  }

  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab?.discarded) {
      await chrome.tabs.reload(tabId);
      await sleep(2500);
    }
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => fetch('https://aisandbox-pa.googleapis.com/v1/credits', { credentials: 'include' }),
    });
    console.log(`[Flow2API] Header sniff triggered on Flow tab ${tabId} (${reason})`);
  } catch (e) {
    console.warn('[Flow2API] Header sniff probe failed:', e?.message || e);
    return hasUsableApiHeaders(entry?.headers);
  }

  const deadline = Date.now() + FLOW_HEADERS_PROBE_WAIT_MS;
  while (Date.now() < deadline) {
    const fresh = _pageApiHeadersByTab.get(tabId);
    if (hasUsableApiHeaders(fresh?.headers)) {
      await syncFlowApiHeaderDnrRules(fresh.headers);
      return true;
    }
    await sleep(200);
  }
  return hasUsableApiHeaders(entry?.headers) || hasUsableApiHeaders(_pageApiHeadersLatest);
}

function resolveFlowReferer(tab, headerSource) {
  const tabUrl = String(tab?.url || '');
  if (tabUrl.includes('labs.google') && tabUrl.includes('/project/')) {
    return tabUrl;
  }
  if (headerSource?.referer) return headerSource.referer;
  if (tabUrl.includes('labs.google')) return tabUrl;
  return FLOW_DEFAULT_REFERER;
}

async function buildFlowApiFetchHeaders(agentHeaders = {}, tabHint = null) {
  const tab = tabHint || await pickPrimaryFlowTab();
  const headerSource = getPageApiHeadersForTab(tab?.id);
  const out = {};

  for (const [lower, value] of Object.entries(headerSource)) {
    if (!value || FLOW_HEADER_SKIP.has(lower)) continue;
    if (lower === 'content-type') continue;
    out[lower] = value;
  }

  out.referer = resolveFlowReferer(tab, headerSource);
  out.origin = headerSource.origin || 'https://labs.google';

  if (!out.accept) out.accept = '*/*';
  if (!out['accept-language']) out['accept-language'] = 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7';
  if (!out.priority) out.priority = 'u=1, i';
  if (!out['sec-fetch-site']) out['sec-fetch-site'] = 'cross-site';
  if (!out['sec-fetch-mode']) out['sec-fetch-mode'] = 'cors';
  if (!out['sec-fetch-dest']) out['sec-fetch-dest'] = 'empty';

  for (const [k, v] of Object.entries(agentHeaders || {})) {
    const lower = String(k || '').toLowerCase();
    if (!v || FLOW_HEADER_SKIP.has(lower) || lower === 'content-type') continue;
    out[lower] = String(v);
  }

  const agentAuth = out.authorization || agentHeaders?.authorization || agentHeaders?.Authorization;
  if (agentAuth) {
    out.authorization = String(agentAuth);
  } else if (flowKey) {
    out.authorization = `Bearer ${flowKey}`;
  }
  return out;
}

function applyFlowPostContentType(fetchHeaders, tabId = null) {
  const headerSource = getPageApiHeadersForTab(tabId);
  const captured = headerSource['content-type'];
  fetchHeaders['content-type'] = (captured && captured.includes('text/plain'))
    ? captured
    : FLOW_POST_CONTENT_TYPE;
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (!tab?.url) return;
  const onFlow = flowUrls.some((pattern) => {
    const re = new RegExp('^' + pattern.replace(/\*/g, '.*') + '$');
    return re.test(tab.url);
  });

  if (info.status === 'complete' && onFlow) {
    _captchaSolvesByTab.set(tabId, 0);
  }
});

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ URL Ă¢â€ â€™ Log Type Classifier Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function classifyUrl(url) {
  if (url.includes('batchGenerateImages'))     return 'GEN_IMG';
  if (url.includes('batchAsyncGenerateVideo')) return 'GEN_VID';
  if (url.includes('batchCheckAsync'))         return 'POLL';
  return 'API';
}

/** Gen image/video submit — bắt buộc captchaToken từ Captcha Center (agent broker). */
function requiresCenterCaptcha(url) {
  if (!url || url.includes('batchCheckAsync')) return false;
  if (url.includes('batchGenerateImages')) return true;
  if (url.includes('batchAsyncGenerateVideo')) return true;
  return false;
}

function bodyHasRecaptchaToken(body) {
  if (!body || typeof body !== 'object') return false;
  if (body.clientContext?.recaptchaContext?.token) return true;
  if (Array.isArray(body.requests)) {
    return body.requests.some((req) => req?.clientContext?.recaptchaContext?.token);
  }
  return false;
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Request Log (last 50 entries) Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

let requestLog = [];

function addRequestLog(entry) {
  requestLog.unshift(entry);
  if (requestLog.length > 50) requestLog.pop();
  broadcastRequestLog();
}

function updateRequestLog(id, updates) {
  const entry = requestLog.find((e) => e.id === id);
  if (entry) Object.assign(entry, updates);
  broadcastRequestLog();
}

function broadcastRequestLog() {
  chrome.runtime.sendMessage({ type: 'REQUEST_LOG_UPDATE', log: requestLog }).catch(() => {});
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Startup Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

chrome.runtime.onInstalled.addListener(init);
chrome.runtime.onStartup.addListener(init);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'reconnect') connectToAgent();
  if (alarm.name === 'keepAlive') keepAlive();
  if (alarm.name === 'flowWatchdog') runFlowWatchdog();
});

async function getOrCreateProfileId() {
  const data = await chrome.storage.local.get(['profileId']);
  if (data.profileId) return data.profileId;
  const id = (crypto?.randomUUID?.() || `p-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  await chrome.storage.local.set({ profileId: id });
  return id;
}

async function getExtensionMode() {
  const { f2apiExtMode } = await chrome.storage.local.get(['f2apiExtMode']);
  return f2apiExtMode === 'center' ? 'center' : 'bridge';
}

async function setDispatchEnabled(enabled, { source = 'agent' } = {}) {
  const next = enabled !== false;
  dispatchEnabled = next;
  await chrome.storage.local.set({ dispatchEnabled: next });
  console.log('[Flow2API] dispatchEnabled =', next, `(${source})`);
  if (next) {
    // Re-enabled: resume token keepalive; open Flow only if none exists.
    try {
      const flowTabs = await chrome.tabs.query({ url: flowUrls });
      if (!flowTabs.length) {
        await openFlowTabResilient(false, { force: true });
      }
    } catch (e) {
      console.warn('[Flow2API] reopen Flow after re-enable failed:', e?.message || e);
    }
    ensureFreshFlowToken('dispatch_reenabled').catch(() => {});
  }
}

function isAutoOpenAllowed() {
  return dispatchEnabled !== false;
}

async function init() {
  const mode = await getExtensionMode();
  console.log('[Flow2API] Extension mode =', mode);

  if (mode === 'center') {
    // Center: chỉ chạy long-poll broker, không WS Bridge / không proxy.
    if (self.__centerLoop?.start) {
      try {
        await self.__centerLoop.start();
      } catch (e) {
        console.error('[Flow2API] center start failed:', e);
      }
    }
    return;
  }
  // Bridge mode (default) — luồng gốc:
  // Note: deliberately not restoring `userInfo` from storage. We used
  // to persist it here, but Google profile fields (name + email) are
  // PII and chrome.storage.local is plaintext + readable by other
  // extensions on the profile that hold the `storage` permission.
  // The agent replays user_info on every WS reconnect anyway via
  // fetchAndPushUserInfo(token), so persistence buys nothing.
  const data = await chrome.storage.local.get([
    'flowKey', 'metrics', 'callbackSecret', 'profileId', 'flowUrl', 'dispatchEnabled',
  ]);
  if (data.flowKey)        flowKey        = data.flowKey;
  if (data.metrics)        Object.assign(metrics, data.metrics);
  if (data.callbackSecret) callbackSecret = data.callbackSecret;
  dispatchEnabled = data.dispatchEnabled !== false;
  profileId = data.profileId || await getOrCreateProfileId();
  await loadCapturedFlowApiHeaders();
  connectToAgent();
  try {
    const flowTabs = await chrome.tabs.query({ url: flowUrls });
    if (!flowTabs.length) {
      if (!isAutoOpenAllowed()) {
        console.log('[Flow2API] Skip auto-open Flow — dispatch disabled');
      } else {
        const url = data.flowUrl || FLOW_URL;
        await openFlowTabResilient(false);
        console.log('[Flow2API] Auto-open Flow on startup:', url);
      }
    }
  } catch (e) {
    console.warn('[Flow2API] Auto-open Flow failed:', e?.message || e);
  }
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
  chrome.alarms.create('flowWatchdog', { periodInMinutes: 2 });
  if (isAutoOpenAllowed()) {
    ensureFreshFlowToken('startup');
  }
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Token Capture Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details?.requestHeaders?.length) return;
    captureFlowApiHeadersFromDetails(details);

    const authHeader = details.requestHeaders.find(
      (h) => h.name?.toLowerCase() === 'authorization',
    );
    const value = authHeader?.value || '';
    if (!value.startsWith('Bearer ya29.')) return;

    const token = value.replace(/^Bearer\s+/i, '').trim();
    if (!token) return;

    // Always update Ă¢â‚¬â€ even if same token string, refresh the timestamp
    const tokenChanged = flowKey !== token;
    flowKey = token;
    metrics.tokenCapturedAt = Date.now();
    chrome.storage.local.set({ flowKey, metrics });

    // Only emit on the WS when the token actually rotated. The listener
    // fires on EVERY outbound aisandbox-pa request Ă¢â‚¬â€ and the agent's
    // own poll loops generate dozens per minute. Re-sending the same
    // string each time pushed the agent into an effective infinite
    // /v1/credits refresh loop (one credits GET per poll). The agent
    // side has a defensive dedupe too, but quiet at the source first.
    if (tokenChanged) {
      console.log('[Flow2API] Bearer token captured');
      pushTokenToAgent(token, metrics.tokenExpiresAt || null);
      // Resolve the user's identity (email/name/picture) once per token Ă¢â‚¬â€
      // saves the popup + AccountPanel from showing "Connected via
      // extension" placeholders. The token already has the userinfo.email
      // + userinfo.profile scopes Flow needs anyway, so this is a free
      // call. Errors are non-fatal and silent.
      fetchAndPushUserInfo(token);
    }
  },
  { urls: ['https://aisandbox-pa.googleapis.com/*', 'https://labs.google/*'] },
  ['requestHeaders', 'extraHeaders'],
);

let cachedUserInfo = null;

async function fetchAndPushUserInfo(token) {
  try {
    const resp = await fetch(
      'https://www.googleapis.com/oauth2/v2/userinfo',
      { headers: { authorization: `Bearer ${token}` } },
    );
    if (!resp.ok) {
      console.warn('[Flow2API] userinfo fetch returned', resp.status);
      return;
    }
    const info = await resp.json();
    // In-memory only Ă¢â‚¬â€ DO NOT persist to chrome.storage.local. PII
    // there is plaintext on disk and readable by other extensions
    // with the `storage` permission. Lifetime = service-worker
    // lifetime; rebuilt on next token rotation if the SW recycles.
    cachedUserInfo = info;
    console.log('[Flow2API] userinfo captured for', info?.email || '<no email>');
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'user_info', userInfo: info }));
    }
  } catch (e) {
    console.warn('[Flow2API] userinfo fetch failed:', e?.message || e);
  }
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ WebSocket to Agent Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function connectToAgent() {
  if (manualDisconnect) return;
  if (ws?.readyState === WebSocket.CONNECTING) return;
  if (ws?.readyState === WebSocket.OPEN) return;

  try {
    ws = new WebSocket(AGENT_WS_URL);
  } catch (e) {
    console.error('[Flow2API] WS connect error:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log('[Flow2API] Connected to agent');
    chrome.alarms.clear('reconnect');
    setState('idle');

    const tokenAge = flowKey && metrics.tokenCapturedAt
      ? Date.now() - metrics.tokenCapturedAt
      : null;

    ws.send(JSON.stringify({
      type: 'extension_ready',
      profileId: profileId,
      profileLabel: cachedUserInfo?.email || cachedUserInfo?.name || '',
      flowKeyPresent: !!flowKey,
      tokenAge,
    }));

    // Resend token immediately so agent can start without waiting for a capture
    if (flowKey) {
      pushTokenToAgent(flowKey, metrics.tokenExpiresAt || null);
    }
    // Replay cached userinfo so the agent's AccountPanel populates on
    // reconnect without waiting for the next token rotation. If we
    // never resolved one yet but a token IS present, kick off a fetch.
    if (cachedUserInfo) {
      ws.send(JSON.stringify({ type: 'user_info', userInfo: cachedUserInfo }));
    } else if (flowKey) {
      fetchAndPushUserInfo(flowKey);
    }
    pushCookiesToAgent();
  };

  ws.onmessage = async ({ data }) => {
    try {
      const msg = JSON.parse(data);

      if (msg.type === 'callback_secret') {
        callbackSecret = msg.secret;
        chrome.storage.local.set({ callbackSecret: msg.secret });
        console.log('[Flow2API] Received callback secret');
      } else if (msg.type === 'pong') {
        // keepalive response Ă¢â‚¬â€ no-op
      } else if (msg.type === 'logout') {
        // Agent's /api/auth/logout invoked Ă¢â‚¬â€ drop in-memory identity
        // so the next reconnect picks up fresh credentials. Don't
        // touch chrome.storage (we don't persist identity there
        // anyway, but be explicit). The WS stays open; agent will
        // re-greet when the user logs back in.
        console.log('[Flow2API] logout requested by agent');
        cachedUserInfo = null;
        flowKey = null;
      } else if (msg.type === 'sync_cookies') {
        const cookies = await exportProfileCookiesForSync();
        if (cookies.length) {
          ws.send(JSON.stringify({ type: 'cookies_synced', cookies }));
        }
        sendToAgent({
          id: msg.id,
          status: 200,
          data: { ok: true, count: cookies.length },
        });
      } else if (msg.type === 'refresh_token') {
        let result = await refreshTokenViaAuthSession(true);
        if (!result.ok) {
          const ok = await ensureFreshFlowToken('server_request', true);
          result = {
            ok,
            flowKeyPresent: !!flowKey,
            method: ok ? 'credits_fallback' : 'failed',
            expiresAt: metrics.tokenExpiresAt || null,
            flowKey: flowKey || null,
            tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
            error: ok ? undefined : (result.error || 'TOKEN_REFRESH_FAILED'),
          };
        }
        sendToAgent({
          id: msg.id,
          status: result.ok ? 200 : 503,
          data: result,
          error: result.ok ? undefined : (result.error || 'TOKEN_REFRESH_FAILED'),
        });
      } else if (msg.type === 'open_flow_tab') {
        try {
          const tabs = await chrome.tabs.query({ url: flowUrls });
          let tabId = null;
          if (tabs.length && tabs[0]?.id) {
            await chrome.tabs.update(tabs[0].id, { active: true });
            tabId = tabs[0].id;
          } else {
            const tab = await openFlowTabResilient(true, { force: true });
            tabId = tab?.id ?? null;
          }
          sendToAgent({
            id: msg.id,
            status: 200,
            data: { ok: true, tabId },
          });
        } catch (e) {
          sendToAgent({
            id: msg.id,
            status: 503,
            data: { ok: false },
            error: e?.message || 'OPEN_FLOW_TAB_FAILED',
          });
        }
      } else if (msg.type === 'please_resend_userinfo') {
        // Agent's /api/auth/scan asks us to re-fetch userinfo when
        // its own cache is empty (e.g. agent restarted, or user
        // clicked "Scan extension" before WS finished its first
        // round-trip). If we have a cached profile, replay it
        // immediately; otherwise refetch from Google's userinfo
        // endpoint with whatever Bearer token we currently hold.
        if (cachedUserInfo) {
          ws.send(JSON.stringify({ type: 'user_info', userInfo: cachedUserInfo }));
        } else if (flowKey) {
          fetchAndPushUserInfo(flowKey);
        } else {
          console.log('[Flow2API] please_resend_userinfo: no token captured yet');
        }
      } else if (msg.type === 'system_force_refresh') {
        if (!isAutoOpenAllowed()) {
          console.log('[Flow2API] Skip force refresh — dispatch disabled');
        } else {
          refreshAllFlowTabs().catch((e) => console.warn('[Flow2API] force refresh failed', e));
        }
      } else if (msg.type === 'system_set_dispatch') {
        setDispatchEnabled(msg.enabled !== false, { source: 'system_set_dispatch' })
          .catch((e) => console.warn('[Flow2API] setDispatchEnabled failed', e));
      } else if (msg.type === 'system_set_proxy') {
        applyProxyConfig(msg.proxyUrl || '');
        chrome.storage.local.set({ proxyUrl: msg.proxyUrl || '' });
      } else if (msg.type === 'system_push_config') {
        applySystemPushConfig(msg.config);
      } else if (msg.method === 'abort_request') {
        // Agent asks us to abort a specific in-flight fetch. Never send a
        // response back — this is fire-and-forget by design so the agent
        // can shed work without a synchronous round-trip. If we haven't
        // registered a controller yet (message ordering race), we still
        // remember the id and bail at the next checkpoint in the handler.
        const targetId =
          msg.params?.targetId
          || msg.params?.id
          || msg.params?.requestId;
        if (targetId) markRequestAborted(targetId);
      } else if (msg.method === 'api_request') {
        await handleApiRequest(msg);
      } else if (msg.method === 'trpc_request') {
        await handleTrpcRequest(msg);
      } else if (msg.method === 'raw_request') {
        await handleRawRequest(msg);
      } else if (msg.method === 'get_status') {
        sendToAgent({
          id: msg.id,
          result: {
            state,
            flowKeyPresent: !!flowKey,
            manualDisconnect,
            tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
            metrics,
          },
        });
      }
    } catch (e) {
      console.error('[Flow2API] Message error:', e);
    }
  };

  ws.onclose = () => {
    setState('off');
    if (!manualDisconnect) scheduleReconnect();
  };

  ws.onerror = (e) => {
    console.error('[Flow2API] WS error:', e);
    metrics.lastError = 'WS_ERROR';
    chrome.storage.local.set({ metrics });
  };
}

function scheduleReconnect() {
  chrome.alarms.create('reconnect', { delayInMinutes: 0.083 }); // ~5 s
}

async function getFlowTabStatus() {
  try {
    const tabs = await chrome.tabs.query({ url: flowUrls });
    return {
      count: tabs.length,
      active: tabs.some((t) => t.active),
      discarded: tabs.filter((t) => t.discarded).length,
      url: tabs[0]?.url || null,
    };
  } catch (e) {
    return { count: 0, active: false, discarded: 0, error: e?.message || String(e) };
  }
}

async function sendHeartbeat() {
  if (ws?.readyState !== WebSocket.OPEN) return;
  const tabStatus = await getFlowTabStatus();
  ws.send(JSON.stringify({
    type: 'heartbeat',
    status: {
      state,
      flowKeyPresent: !!flowKey,
      tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      metrics,
      flowTab: tabStatus,
      userInfo: cachedUserInfo,
    },
  }));
}

function keepAlive() {
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
    sendHeartbeat();
  } else {
    connectToAgent();
  }
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Send to Agent Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

/**
 * Route a message to the agent.
 * Responses (msg.id present) go via HTTP callback Ă¢â‚¬â€ immune to WS drops.
 * Falls back to WS on HTTP failure. Non-response messages use WS directly.
 */
function sendToAgent(msg) {
  if (msg.id) {
    fetch(CALLBACK_URL, {
      method:  'POST',
      headers: {
        'Content-Type':      'application/json',
        'X-Callback-Secret': callbackSecret || '',
      },
      body: JSON.stringify(msg),
    }).catch(() => {
      // HTTP failed Ă¢â‚¬â€ fall back to WS
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
    });
    return;
  }
  // Non-response messages (ping, status, token_captured)
  if (ws?.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ API Request Proxy Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

async function handleApiRequest(msg) {
  const { id, params } = msg;
  // `captchaToken` — do Python agent CaptchaBroker cấp sẵn. Bridge chỉ inject.
  const { url, method, headers, body, captchaAction } = params || {};
  const preSuppliedCaptchaToken = typeof params?.captchaToken === 'string' && params.captchaToken
    ? String(params.captchaToken)
    : null;
  const mustHaveCenterCaptcha = requiresCenterCaptcha(url);

  if (!url || !url.startsWith('https://aisandbox-pa.googleapis.com/')) {
    sendToAgent({ id, status: 400, error: 'INVALID_URL' });
    return;
  }

  // Agent may have canceled this task before the message reached us.
  // Bail immediately: no captcha, no fetch, no credit burn.
  if (isRequestAborted(id)) {
    sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
    clearAbortTracking(id);
    return;
  }

  setState('running');
  const hasCaptcha = !!(mustHaveCenterCaptcha || captchaAction || preSuppliedCaptchaToken);
  if (hasCaptcha) metrics.requestCount++;

  addRequestLog({
    id,
    type:   classifyUrl(url),
    time:   new Date().toISOString(),
    status: 'processing',
    url,
  });

  try {
    const agentHeaders = headers || {};
    const agentAuth = agentHeaders.authorization || agentHeaders.Authorization;
    const usingDbToken = !!(agentAuth && String(agentAuth).match(/^Bearer\s+/i));

    // Step 0: Fail fast if we have no bearer token (DB token from agent, or legacy browser capture).
    if (!usingDbToken && !flowKey) {
      sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_FLOW_KEY'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'NO_FLOW_KEY' });
      setState('idle');
      return;
    }

    // Step 1: Chỉ refresh token trên browser khi agent không gửi token từ DB.
    if (!usingDbToken) {
      const freshEnough = await ensureFreshFlowToken('api_request');
      if (!freshEnough || !flowKey) {
        sendToAgent({ id, status: 503, error: 'TOKEN_REFRESH_FAILED' });
        if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'TOKEN_REFRESH_FAILED'; }
        chrome.storage.local.set({ metrics });
        updateRequestLog(id, { status: 'failed', error: 'TOKEN_REFRESH_FAILED' });
        setState('idle');
        return;
      }
    }

    // Gate #1: bail before spending a captcha solve if agent already
    // canceled us. captcha solves are rate-limited AND single-use so
    // skipping them here is the biggest early-exit win.
    if (isRequestAborted(id)) {
      sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
      updateRequestLog(id, { status: 'failed', error: 'REQUEST_CANCELED' });
      setState('idle');
      return;
    }

    // Step 2: Captcha token — Bridge chỉ inject token do Captcha Center cấp (agent broker).
    // Không tự solve trên tab Flow của profile worker (tránh burn score trên nhiều tab).
    let captchaToken = preSuppliedCaptchaToken;
    let headerTab = await pickPrimaryFlowTab();
    if (mustHaveCenterCaptcha && !captchaToken) {
      console.error('[Flow2API] Blocked gen image/video — missing Center captchaToken');
      sendToAgent({ id, status: 503, error: 'NO_CAPTCHA_CENTER' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_CAPTCHA_CENTER'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'NO_CAPTCHA_CENTER' });
      setState('idle');
      return;
    }
    if (captchaAction && !captchaToken) {
      console.error(`[Flow2API] Missing captchaToken from Center for action=${captchaAction}`);
      sendToAgent({ id, status: 503, error: 'NO_CAPTCHA_CENTER' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_CAPTCHA_CENTER'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'NO_CAPTCHA_CENTER' });
      setState('idle');
      return;
    }
    if (captchaToken) {
      console.log(`[Flow2API] reCAPTCHA received from Center (len=${captchaToken.length})`);
      const pidFromBody = extractProjectIdFromBody(body);
      if (pidFromBody) noteProjectId(pidFromBody);
    }

    // Gate #2: last chance before the fetch actually reaches Google.
    // Everything past this point can burn Flow credits, so this gate
    // is the credit-saving one.
    if (isRequestAborted(id)) {
      sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
      updateRequestLog(id, { status: 'failed', error: 'REQUEST_CANCELED' });
      setState('idle');
      return;
    }

    // Step 2: Inject captcha token into body clone if present
    let finalBody = body;
    if (captchaToken && finalBody) {
      finalBody = JSON.parse(JSON.stringify(finalBody)); // deep clone
      if (finalBody.clientContext?.recaptchaContext) {
        finalBody.clientContext.recaptchaContext.token = captchaToken;
      }
      if (finalBody.requests && Array.isArray(finalBody.requests)) {
        for (const req of finalBody.requests) {
          if (req.clientContext?.recaptchaContext) {
            req.clientContext.recaptchaContext.token = captchaToken;
          }
        }
      }
    }

    if (mustHaveCenterCaptcha && finalBody && !bodyHasRecaptchaToken(finalBody)) {
      console.error('[Flow2API] Blocked gen image/video — captchaToken not injected into body');
      sendToAgent({ id, status: 503, error: 'NO_CAPTCHA_CENTER' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_CAPTCHA_CENTER'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'NO_CAPTCHA_CENTER' });
      setState('idle');
      return;
    }

    if (headerTab?.id) {
      await ensureFlowApiHeadersFromTab(headerTab.id, captchaAction ? 'post_captcha' : 'api_request');
    }

    const fetchHeaders = await buildFlowApiFetchHeaders(headers, headerTab);
    let requestBody;
    if (method !== 'GET') {
      const isPlainObject = finalBody && typeof finalBody === 'object' && !(finalBody instanceof FormData) && !(finalBody instanceof Blob) && !(finalBody instanceof ArrayBuffer);
      requestBody = isPlainObject ? JSON.stringify(finalBody) : finalBody;
      if (isPlainObject) {
        applyFlowPostContentType(fetchHeaders, headerTab?.id ?? null);
      }
    }

    // Wire an AbortController so an abort message arriving mid-fetch
    // (after Google has been contacted) can tear the socket down. If
    // abort arrived between the gate above and here, propagate now
    // instead of firing the request.
    const controller = new AbortController();
    registerAbortController(id, controller);
    if (isRequestAborted(id)) controller.abort();

    let response = await fetch(url, {
      method:      method || 'POST',
      headers:     fetchHeaders,
      credentials: 'include',
      body:        requestBody,
      signal:      controller.signal,
    });

    if (response.status === 401 && !usingDbToken) {
      console.warn('[Flow2API] API_401 - refreshing token and retrying once');
      const refreshed = await ensureFreshFlowToken('api_401_retry', true);
      if (refreshed && flowKey && !isRequestAborted(id)) {
        fetchHeaders.authorization = `Bearer ${flowKey}`;
        response = await fetch(url, {
          method:      method || 'POST',
          headers:     fetchHeaders,
          credentials: 'include',
          body:        requestBody,
          signal:      controller.signal,
        });
      }
    }

    const responseText = await response.text();
    let responseData;
    try {
      responseData = JSON.parse(responseText);
    } catch {
      responseData = responseText;
    }

    sendToAgent({ id, status: response.status, data: responseData });

    if (response.ok) {
      if (hasCaptcha) { metrics.successCount++; metrics.lastError = null; }
      updateRequestLog(id, { status: 'success', httpStatus: response.status });
    } else {
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `API_${response.status}`; }
      updateRequestLog(id, { status: 'failed', httpStatus: response.status, error: `API_${response.status}` });
    }
  } catch (e) {
    // fetch throws DOMException("AbortError") when controller.abort()
    // fires. Report a distinct 499 so the agent (or a later reviewer
    // reading logs) can tell "user canceled" apart from "network died".
    if (e?.name === 'AbortError' || isRequestAborted(id)) {
      sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
      updateRequestLog(id, { status: 'failed', error: 'REQUEST_CANCELED' });
    } else {
      sendToAgent({ id, status: 500, error: e.message || 'API_REQUEST_FAILED' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = e.message || 'API_REQUEST_FAILED'; }
      updateRequestLog(id, { status: 'failed', error: e.message || 'API_REQUEST_FAILED' });
    }
  } finally {
    clearAbortTracking(id);
  }

  chrome.storage.local.set({ metrics });
  setState('idle');
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Token Refresh (minimal) Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

let _openingFlowTab = false;
let _lastFlowWatchdogReloadAt = 0;
let _flowWatchdogRunning = false;

const FLOW_URL = 'https://labs.google/fx/vi/tools/flow';

let proxyAuthCredentials = null;

function proxyAuthHandler(details) {
  if (details.isProxy && proxyAuthCredentials) {
    return { authCredentials: proxyAuthCredentials };
  }
}

function applyProxyConfig(proxyUrl) {
  if (!proxyUrl) {
    chrome.proxy.settings.clear({ scope: 'regular' });
    if (chrome.webRequest?.onAuthRequired?.hasListener?.(proxyAuthHandler)) {
      chrome.webRequest.onAuthRequired.removeListener(proxyAuthHandler);
    }
    proxyAuthCredentials = null;
    return;
  }
  const parts = String(proxyUrl).split(':');
  const host = parts[0];
  const port = parseInt(parts[1], 10) || 80;
  const username = parts[2] || null;
  const password = parts[3] || null;
  chrome.proxy.settings.set({
    value: {
      mode: 'fixed_servers',
      rules: {
        singleProxy: { scheme: 'http', host, port },
        bypassList: ['localhost', '127.0.0.1'],
      },
    },
    scope: 'regular',
  });
  if (chrome.webRequest?.onAuthRequired?.hasListener?.(proxyAuthHandler)) {
    chrome.webRequest.onAuthRequired.removeListener(proxyAuthHandler);
  }
  if (username && password) {
    proxyAuthCredentials = { username, password };
    chrome.webRequest.onAuthRequired.addListener(
      proxyAuthHandler,
      { urls: ['<all_urls>'] },
      ['blocking'],
    );
  }
}

async function refreshAllFlowTabs() {
  const tabs = await chrome.tabs.query({
    url: ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*', 'https://labs.google/fx', 'https://labs.google/fx/*'],
  });
  for (const tab of tabs) {
    if (tab.id != null) {
      try { await chrome.tabs.reload(tab.id); } catch { /* ignore */ }
    }
  }
}

async function applySystemPushConfig(config) {
  if (!config || typeof config !== 'object') return;
  if (config.flowUrl) {
    await chrome.storage.local.set({ flowUrl: config.flowUrl });
  }
}

function noteProjectId(projectId) {
  const id = String(projectId || '').trim();
  if (!id || id === 'new') return;
  if (_cachedProjectId !== id) {
    _cachedProjectId = id;
    console.log('[Flow2API] Cached projectId:', id.slice(0, 12) + '…');
  }
}

function extractProjectIdFromBody(body) {
  if (!body || typeof body !== 'object') return null;
  const direct = body.clientContext?.projectId;
  if (direct) return String(direct);
  if (Array.isArray(body.requests)) {
    for (const req of body.requests) {
      const pid = req?.clientContext?.projectId;
      if (pid) return String(pid);
    }
  }
  return null;
}

/**
 * Open a Flow tab even when Chrome has zero windows. `chrome.tabs.create`
 * throws "No current window" in that state because it needs a window
 * context to attach to; `chrome.windows.create` spawns a fresh window
 * and tab in one call. Falls back through both paths so we recover from
 * "all-windows-closed but service-worker-still-alive" silently.
 *
 * @param {boolean} active
 * @param {{ force?: boolean }} [opts] force=true bypasses dispatch-disabled gate
 *   (explicit user/agent open). Auto paths must leave force unset/false.
 */
async function openFlowTabResilient(active = false, opts = {}) {
  const force = !!(opts && opts.force);
  if (!force && !isAutoOpenAllowed()) {
    console.log('[Flow2API] Blocked auto-open Flow — dispatch disabled');
    return null;
  }
  try {
    return await chrome.tabs.create({ url: FLOW_URL, active });
  } catch (e) {
    const msg = e?.message || '';
    if (!msg.includes('No current window')) throw e;
    console.log('[Flow2API] No Chrome window — spawning a fresh one for Flow');
    const win = await chrome.windows.create({
      url: FLOW_URL,
      focused: false,
      state: 'minimized',
    });
    return win.tabs?.[0] ?? null;
  }
}

async function captureTokenFromFlowTab() {
  let tabs = await chrome.tabs.query({
    url: ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*'],
  });

  if (!tabs.length) {
    if (_openingFlowTab) return false;
    _openingFlowTab = true;
    try {
      console.log('[Flow2API] No Flow tab - opening in background');
      await openFlowTabResilient(false);
      await sleep(3000);
      tabs = await chrome.tabs.query({ url: flowUrls });
    } catch (e) {
      console.error('[Flow2API] Failed to open Flow tab:', e);
      return false;
    } finally {
      _openingFlowTab = false;
    }
  }

  let target = tabs.find((tab) => tab?.id && !tab.discarded) || null;
  if (!target?.id) {
    if (_openingFlowTab) return !!flowKey;
    _openingFlowTab = true;
    try {
      console.log('[Flow2API] Only discarded Flow tabs — opening a fresh background tab for token capture');
      await openFlowTabResilient(false);
      await sleep(3000);
      const fresh = await chrome.tabs.query({ url: flowUrls });
      target = fresh.find((tab) => tab?.id && !tab.discarded) || null;
    } catch (e) {
      console.error('[Flow2API] Failed to open Flow tab for token:', e);
      return false;
    } finally {
      _openingFlowTab = false;
    }
  }
  if (!target?.id) return !!flowKey;

  try {
    // Trigger real aisandbox-pa traffic so webRequest sees a fresh Authorization header.
    await chrome.scripting.executeScript({
      target: { tabId: target.id },
      func:   () => fetch('https://aisandbox-pa.googleapis.com/v1/credits', { credentials: 'include' }),
    });
    console.log('[Flow2API] Token refresh triggered on Flow tab');
    return true;
  } catch (e) {
    console.error('[Flow2API] Token refresh failed:', e);
    return false;
  }
}

async function runFlowWatchdog() {
  if (_flowWatchdogRunning) return;
  if (state === 'running') return;
  if (!isAutoOpenAllowed()) return;

  const now = Date.now();
  const tokenAge = metrics.tokenCapturedAt ? now - metrics.tokenCapturedAt : Infinity;
  if (flowKey && tokenAge < FLOW_WATCHDOG_STALE_MS) return;
  if (now - _lastFlowWatchdogReloadAt < FLOW_WATCHDOG_RELOAD_COOLDOWN_MS) return;

  _flowWatchdogRunning = true;
  try {
    console.log('[Flow2API] Watchdog refreshing stale/missing Flow token');
    _lastFlowWatchdogReloadAt = now;
    await ensureFreshFlowToken('watchdog', true);
  } catch (e) {
    console.warn('[Flow2API] Watchdog refresh failed:', e?.message || e);
  } finally {
    _flowWatchdogRunning = false;
  }
}

function pushTokenToAgent(token, expiresAtIso) {
  if (!token || ws?.readyState !== WebSocket.OPEN) return;
  const payload = { type: 'token_captured', flowKey: token };
  if (expiresAtIso) payload.expiresAt = expiresAtIso;
  ws.send(JSON.stringify(payload));
  pushCookiesToAgent();
}

const COOKIE_SYNC_DOMAINS = [
  'labs.google',
  '.labs.google',
  '.google.com',
  'google.com',
  'accounts.google.com',
  '.accounts.google.com',
];

async function exportProfileCookiesForSync() {
  const seen = new Set();
  const cookies = [];
  for (const domain of COOKIE_SYNC_DOMAINS) {
    try {
      const list = await chrome.cookies.getAll({ domain });
      for (const c of list) {
        const key = `${c.name}|${c.domain}|${c.path || '/'}`;
        if (seen.has(key)) continue;
        seen.add(key);
        cookies.push({
          name: c.name,
          value: c.value,
          domain: c.domain,
          path: c.path || '/',
        });
      }
    } catch (e) {
      console.warn('[Flow2API] cookie export failed for', domain, e?.message || e);
    }
  }
  return cookies;
}

async function pushCookiesToAgent() {
  if (ws?.readyState !== WebSocket.OPEN) return;
  try {
    const cookies = await exportProfileCookiesForSync();
    if (!cookies.length) return;
    ws.send(JSON.stringify({ type: 'cookies_synced', cookies }));
  } catch (e) {
    console.warn('[Flow2API] pushCookiesToAgent failed:', e?.message || e);
  }
}

async function refreshTokenViaAuthSession(forceOpen = false) {
  let tabs = await chrome.tabs.query({ url: flowUrls });
  if (!tabs.length) {
    if (_openingFlowTab) {
      return { ok: false, error: 'FLOW_TAB_OPENING', flowKeyPresent: !!flowKey };
    }
    _openingFlowTab = true;
    try {
      await openFlowTabResilient(forceOpen, { force: !!forceOpen });
      await sleep(3000);
      tabs = await chrome.tabs.query({ url: flowUrls });
    } catch (e) {
      return { ok: false, error: e?.message || 'OPEN_FLOW_TAB_FAILED', flowKeyPresent: !!flowKey };
    } finally {
      _openingFlowTab = false;
    }
  }
  const target = tabs.find((tab) => tab?.id && !tab.discarded) || null;
  if (!target?.id) {
    return { ok: false, error: 'NO_FLOW_TAB', flowKeyPresent: !!flowKey };
  }
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: target.id },
      func: async () => {
        const resp = await fetch('https://labs.google/fx/api/auth/session', {
          credentials: 'include',
          headers: { accept: 'application/json' },
        });
        let body = null;
        try {
          body = await resp.json();
        } catch {
          body = null;
        }
        return { ok: resp.ok, status: resp.status, body };
      },
    });
    const result = results?.[0]?.result;
    const body = result?.body;
    if (!result?.ok || !body || Array.isArray(body) || !body.access_token) {
      return {
        ok: false,
        error: 'AUTH_SESSION_EMPTY',
        flowKeyPresent: !!flowKey,
        status: result?.status,
      };
    }
    const expiresAtIso = body.expires ? new Date(body.expires).toISOString() : null;
    flowKey = body.access_token;
    metrics.tokenCapturedAt = Date.now();
    metrics.tokenExpiresAt = expiresAtIso;
    chrome.storage.local.set({ flowKey, metrics });
    pushTokenToAgent(flowKey, expiresAtIso);
    fetchAndPushUserInfo(flowKey);
    return {
      ok: true,
      flowKeyPresent: true,
      method: 'auth_session',
      expiresAt: expiresAtIso,
      flowKey,
      tokenAge: 0,
    };
  } catch (e) {
    return {
      ok: false,
      error: e?.message || 'AUTH_SESSION_ERROR',
      flowKeyPresent: !!flowKey,
    };
  }
}

async function ensureFreshFlowToken(reason = 'request', force = false) {
  const beforeToken = flowKey;
  const beforeCapturedAt = metrics.tokenCapturedAt || 0;
  const age = beforeCapturedAt ? Date.now() - beforeCapturedAt : Infinity;
  if (!force && flowKey && age < TOKEN_SOFT_MAX_AGE_MS) return true;

  const sessionResult = await refreshTokenViaAuthSession(false);
  if (sessionResult.ok) return true;

  const triggered = await captureTokenFromFlowTab();
  if (!triggered) return !!flowKey;

  const deadline = Date.now() + TOKEN_REFRESH_WAIT_MS;
  while (Date.now() < deadline) {
    if (flowKey && (flowKey !== beforeToken || (metrics.tokenCapturedAt || 0) > beforeCapturedAt)) {
      pushTokenToAgent(flowKey, metrics.tokenExpiresAt || null);
      return true;
    }
    await sleep(250);
  }

  // If token did not rotate but still exists, keep going; Google may reuse token string.
  return !!flowKey;
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ reCAPTCHA Solving Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function runCaptchaExclusive(fn) {
  const run = _captchaChain.then(fn, fn);
  _captchaChain = run.catch(() => {});
  return run;
}

function noteCaptchaSolveOnTab(tabId) {
  _captchaSolvesByTab.set(tabId, (_captchaSolvesByTab.get(tabId) || 0) + 1);
}

async function requestCaptchaFromTab(tabId, requestId, pageAction) {
  try {
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  } catch (error) {
    const msg = error?.message || '';
    const shouldInject =
      msg.includes('Receiving end does not exist') ||
      msg.includes('Could not establish connection');
    if (!shouldInject) throw error;

    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['content.js'],
    });
    await sleep(200);
    return await chrome.tabs.sendMessage(tabId, {
      type: 'GET_CAPTCHA',
      requestId,
      pageAction,
    });
  }
}

/** Skip discarded tabs — never reload the user's open Flow/project page. */
function usableFlowTab(tab) {
  if (!tab?.id || tab.discarded) return null;
  return tab;
}

async function solveCaptcha(requestId, captchaAction) {
  return runCaptchaExclusive(async () => {
  const tabs = await chrome.tabs.query({ url: flowUrls });

  // No Flow tab at all — spawn one (handles "no Chrome window" via the
  // resilient helper).
  if (!tabs.length) {
    try {
      await openFlowTabResilient(false);
      await sleep(3000);
    } catch (e) {
      return { error: e.message || 'NO_FLOW_TAB' };
    }
  }

  // Try each live Flow tab — skip discarded tabs instead of reloading them.
  const candidates = await chrome.tabs.query({ url: flowUrls });
  const errors = [];
  for (const tab of candidates) {
    const live = usableFlowTab(tab);
    if (!live) continue;
    try {
      const resp = await Promise.race([
        requestCaptchaFromTab(live.id, requestId, captchaAction),
        new Promise((_, rej) => setTimeout(() => rej(new Error('CAPTCHA_TIMEOUT')), 30000)),
      ]);
      if (resp?.token) {
        noteCaptchaSolveOnTab(live.id);
        return { ...resp, tabId: live.id };
      }
      return resp;
    } catch (e) {
      const msg = e?.message || '';
      errors.push(msg);
      if (
        msg.includes('No current window') ||
        msg.includes('No tab with id') ||
        msg.includes('Receiving end does not exist')
      ) {
        continue;
      }
      return { error: msg };
    }
  }

  // All live candidates failed — open a fresh background Flow tab and try once.
  try {
    await openFlowTabResilient(false);
    await sleep(3000);
    const fresh = await chrome.tabs.query({ url: flowUrls });
    const target = fresh.find((t) => t?.id && !t.discarded) || null;
    if (!target) return { error: 'NO_FLOW_TAB' };
    const resp = await Promise.race([
      requestCaptchaFromTab(target.id, requestId, captchaAction),
      new Promise((_, rej) => setTimeout(() => rej(new Error('CAPTCHA_TIMEOUT')), 30000)),
    ]);
    if (resp?.token) {
      noteCaptchaSolveOnTab(target.id);
      return { ...resp, tabId: target.id };
    }
    return resp;
  } catch (e) {
    const msg = e?.message || (errors[0] ?? 'NO_FLOW_TAB');
    return { error: msg };
  }
  });
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ TRPC Request Proxy Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function isAllowedTrpcUrl(url) {
  return !!url && (
    url.startsWith('https://labs.google/fx/api/trpc/')
    || url.startsWith('https://labs.google/fx/api/upload-video')
  );
}

function isAllowedRawUrl(url) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:') return false;
    const host = u.hostname.toLowerCase();
    return host === 'storage.googleapis.com'
      || host === 'flow-content.google'
      || host.endsWith('.googleapis.com')
      || host.endsWith('.googleusercontent.com');
  } catch {
    return false;
  }
}

async function handleTrpcRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'POST', headers = {}, body, redirect = 'follow' } = params;

  if (!isAllowedTrpcUrl(url)) {
    sendToAgent({ id, error: 'INVALID_TRPC_URL' });
    return;
  }

  if (isRequestAborted(id)) {
    sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
    clearAbortTracking(id);
    return;
  }

  setState('running');

  const agentAuth = headers?.authorization || headers?.Authorization;
  if (!agentAuth && !flowKey) {
    sendToAgent({ id, error: 'NO_FLOW_KEY' });
    return;
  }

  const fetchHeaders = { ...headers };
  if (!agentAuth && flowKey) {
    fetchHeaders['authorization'] = `Bearer ${flowKey}`;
  }
  const hasBody = body !== undefined && body !== null;
  if (hasBody && !Object.keys(fetchHeaders).some((k) => k.toLowerCase() === 'content-type')) {
    fetchHeaders['Content-Type'] = 'application/json';
  }

  const controller = new AbortController();
  registerAbortController(id, controller);
  if (isRequestAborted(id)) controller.abort();

  try {
    const resp = await fetch(url, {
      method,
      headers: fetchHeaders,
      body: hasBody ? JSON.stringify(body) : undefined,
      credentials: 'include',
      signal: controller.signal,
      redirect: redirect === 'manual' ? 'manual' : 'follow',
    });
    const location = resp.headers.get('Location') || resp.headers.get('location') || '';
    const isRedirect =
      resp.type === 'opaqueredirect'
      || resp.status === 0
      || (resp.status >= 300 && resp.status < 400);
    let data = null;
    if (!isRedirect) {
      const text = await resp.text();
      try {
        data = text ? JSON.parse(text) : {};
      } catch {
        data = text;
      }
    }
    sendToAgent({
      id,
      status: resp.status,
      data,
      headers: { location },
      url: resp.url,
      type: resp.type,
    });
    if (resp.ok && url.includes('project.createProject')) {
      try {
        const pid = data?.result?.data?.json?.result?.projectId;
        if (pid) noteProjectId(pid);
      } catch {
        /* ignore parse errors */
      }
    }
  } catch (e) {
    if (e?.name === 'AbortError' || isRequestAborted(id)) {
      sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
    } else {
      console.error('[Flow2API] tRPC request failed:', e);
      sendToAgent({ id, error: e.message || 'TRPC_FETCH_FAILED' });
    }
  } finally {
    clearAbortTracking(id);
    setState('idle');
  }
}

async function handleRawRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'GET', headers = {}, bodyBase64 } = params || {};

  if (!url || !isAllowedRawUrl(url)) {
    sendToAgent({ id, status: 400, error: 'INVALID_RAW_URL' });
    return;
  }

  const agentAuth = headers?.authorization || headers?.Authorization;
  if (!agentAuth && !flowKey) {
    sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
    return;
  }

  if (isRequestAborted(id)) {
    sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
    clearAbortTracking(id);
    return;
  }

  setState('running');
  const controller = new AbortController();
  registerAbortController(id, controller);
  if (isRequestAborted(id)) controller.abort();

  try {
    const fetchHeaders = { ...(headers || {}) };
    const authKey = Object.keys(fetchHeaders).find((k) => k.toLowerCase() === 'authorization');
    if (!authKey) {
      fetchHeaders.authorization = `Bearer ${flowKey}`;
    }
    let body;
    if (bodyBase64) {
      const bin = atob(bodyBase64);
      const bytes = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      body = bytes;
    }
    const resp = await fetch(url, {
      method: method || 'PUT',
      headers: fetchHeaders,
      body,
      credentials: 'include',
      signal: controller.signal,
    });
    const text = await resp.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = text;
    }
    sendToAgent({ id, status: resp.status, data });
  } catch (e) {
    if (e?.name === 'AbortError' || isRequestAborted(id)) {
      sendToAgent({ id, status: 499, error: 'REQUEST_CANCELED' });
    } else {
      console.error('[Flow2API] raw request failed:', e);
      sendToAgent({ id, status: 500, error: e.message || 'RAW_REQUEST_FAILED' });
    }
  } finally {
    clearAbortTracking(id);
    setState('idle');
  }
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ State & Badge Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function setState(newState) {
  state = newState;
  chrome.action.setBadgeText({ text: '' });
  broadcastStatus();
}

function broadcastStatus() {
  chrome.runtime.sendMessage({ type: 'STATUS_PUSH' }).catch(() => {});
}

// ─── Popup Message Handlers ───────────────────────────────────────────────────Ă¢â€â‚¬

chrome.runtime.onMessage.addListener((msg, _, reply) => {
  if (msg.type === 'STATUS') {
    // Service workers can sleep/restart: token survives in storage,
    // cachedUserInfo does not. If popup opens in that state, refetch
    // immediately so account stops showing 'chưa nhận diện'.
    if (flowKey && !cachedUserInfo) {
      fetchAndPushUserInfo(flowKey).then(() => broadcastStatus()).catch(() => {});
    }
    reply({
      connected:       ws?.readyState === WebSocket.OPEN,
      profileId:       profileId,
      flowKeyPresent:  !!flowKey,
      manualDisconnect,
      tokenAge:        metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
      metrics: {
        requestCount: metrics.requestCount,
        successCount: metrics.successCount,
        failedCount:  metrics.failedCount,
        lastError:    metrics.lastError,
      },
      userInfo: cachedUserInfo,
      state,
    });
    return true;
  }

  if (msg.type === 'DISCONNECT') {
    manualDisconnect = true;
    ws?.close();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'RECONNECT') {
    manualDisconnect = false;
    connectToAgent();
    reply({ ok: true });
    return true;
  }

  if (msg.type === 'REQUEST_LOG') {
    reply({ log: requestLog });
    return true;
  }

  if (msg.type === 'OPEN_FLOW_TAB') {
    chrome.tabs.query({
      url: ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*'],
    }).then(async (tabs) => {
      try {
        if (tabs.length) {
          await chrome.tabs.update(tabs[0].id, { active: true });
          reply({ ok: true, tabId: tabs[0].id });
        } else {
          // User-initiated → focus the new window so they can see it.
          const tab = await openFlowTabResilient(true, { force: true });
          reply({ ok: true, tabId: tab?.id });
        }
      } catch (e) {
        reply({ error: e.message });
      }
    }).catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'REFRESH_TOKEN') {
    ensureFreshFlowToken('popup', true)
      .then((ok) => reply({ ok }))
      .catch((e) => reply({ error: e.message }));
    return true;
  }

  if (msg.type === 'RESTART_CENTER') {
    (async () => {
      const mode = await getExtensionMode();
      if (mode !== 'center') {
        reply({ ok: false, error: 'not_center_mode' });
        return;
      }
      if (self.__centerLoop?.restart) {
        await self.__centerLoop.restart('popup');
        reply({ ok: true });
      } else {
        reply({ ok: false, error: 'center_loop_unavailable' });
      }
    })().catch((e) => reply({ error: e.message }));
    return true;
  }

  return true;
});

console.log('[Flow2API] Extension loaded');
void init();

