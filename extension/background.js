/**
 * Flow2API Bridge Ă¢â‚¬â€ Chrome Extension Background Service Worker
 *
 * Connects to local Python agent via WebSocket (agent runs WS server).
 * Captures Bearer token and proxies API calls through the browser context.
 */

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

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (!tab?.url) return;
  const onFlow = flowUrls.some((pattern) => {
    const re = new RegExp('^' + pattern.replace(/\*/g, '.*') + '$');
    return re.test(tab.url);
  });

  if (info.status === 'complete' && onFlow) {
    _captchaSolvesByTab.set(tabId, 0);
  }

  if (clearState.running && clearState.tabId === tabId) {
    const url = info.url || tab.url;
    if (url && !isFlowProjectUrl(url)) {
      stopAutoClear(false).catch(() => {});
      return;
    }
  }

  if ((info.status === 'complete' || info.url) && isFlowProjectUrl(tab.url)) {
    chrome.storage.local.get(['f2apiClearUserStopped']).then((data) => {
      if (data.f2apiClearUserStopped) return;
      loadClearState().then(() => {
        if (!clearState.running) ensureAutoClearStarted().catch(() => {});
      });
    });
  }
});

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ URL Ă¢â€ â€™ Log Type Classifier Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

function classifyUrl(url) {
  if (url.includes('batchGenerateImages'))     return 'GEN_IMG';
  if (url.includes('batchAsyncGenerateVideo')) return 'GEN_VID';
  if (url.includes('batchCheckAsync'))         return 'POLL';
  return 'API';
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
  if (alarm.name === CLEAR_ALARM) {
    loadClearState().then(() => performClearTick());
  }
});

async function getOrCreateProfileId() {
  const data = await chrome.storage.local.get(['profileId']);
  if (data.profileId) return data.profileId;
  const id = (crypto?.randomUUID?.() || `p-${Date.now()}-${Math.random().toString(16).slice(2)}`);
  await chrome.storage.local.set({ profileId: id });
  return id;
}

async function init() {
  // Note: deliberately not restoring `userInfo` from storage. We used
  // to persist it here, but Google profile fields (name + email) are
  // PII and chrome.storage.local is plaintext + readable by other
  // extensions on the profile that hold the `storage` permission.
  // The agent replays user_info on every WS reconnect anyway via
  // fetchAndPushUserInfo(token), so persistence buys nothing.
  const data = await chrome.storage.local.get(['flowKey', 'metrics', 'callbackSecret', 'profileId', 'flowUrl']);
  if (data.flowKey)        flowKey        = data.flowKey;
  if (data.metrics)        Object.assign(metrics, data.metrics);
  if (data.callbackSecret) callbackSecret = data.callbackSecret;
  profileId = data.profileId || await getOrCreateProfileId();
  chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [9001] }).catch(() => {});
  connectToAgent();
  try {
    const flowTabs = await chrome.tabs.query({ url: flowUrls });
    if (!flowTabs.length) {
      const url = data.flowUrl || FLOW_URL;
      await openFlowTabResilient(false);
      console.log('[Flow2API] Auto-open Flow on startup:', url);
    }
  } catch (e) {
    console.warn('[Flow2API] Auto-open Flow failed:', e?.message || e);
  }
  chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
  chrome.alarms.create('flowWatchdog', { periodInMinutes: 2 });
  ensureFreshFlowToken('startup');
  await chrome.storage.local.set({ f2apiClearUserStopped: false });
  const prefs = await chrome.storage.local.get(['autoClickCreateFlow']);
  if (prefs.autoClickCreateFlow === undefined) {
    await chrome.storage.local.set({ autoClickCreateFlow: true });
  }
  await initClearFromStorage();
  await ensureAutoClearStarted();
}

// Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬ Token Capture Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬Ă¢â€â‚¬

chrome.webRequest.onBeforeSendHeaders.addListener(
  (details) => {
    if (!details?.requestHeaders?.length) return;

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
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
      }
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
      ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
    }
    // Replay cached userinfo so the agent's AccountPanel populates on
    // reconnect without waiting for the next token rotation. If we
    // never resolved one yet but a token IS present, kick off a fetch.
    if (cachedUserInfo) {
      ws.send(JSON.stringify({ type: 'user_info', userInfo: cachedUserInfo }));
    } else if (flowKey) {
      fetchAndPushUserInfo(flowKey);
    }
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
      } else if (msg.type === 'refresh_token') {
        const ok = await ensureFreshFlowToken('server_request', true);
        sendToAgent({
          id: msg.id,
          status: ok ? 200 : 503,
          data: {
            ok,
            flowKeyPresent: !!flowKey,
            tokenAge: metrics.tokenCapturedAt ? Date.now() - metrics.tokenCapturedAt : null,
          },
          error: ok ? undefined : 'TOKEN_REFRESH_FAILED',
        });
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
        refreshAllFlowTabs().catch((e) => console.warn('[Flow2API] force refresh failed', e));
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
      } else if (msg.method === 'clear_control') {
        const action = String(msg.params?.action || '').toLowerCase();
        const intervalSec = msg.params?.intervalSec;
        try {
          let result;
          if (action === 'get_state') {
            await loadClearState();
            result = { ok: true, state: getPublicClearState() };
          } else if (action === 'start') {
            await chrome.storage.local.set({ f2apiClearUserStopped: false });
            const res = await startAutoClear(
              intervalSec || clearState.intervalSec || AUTO_CLEAR_INTERVAL_SEC,
              null,
            );
            result = res.ok
              ? { ok: true, state: getPublicClearState(), ...res }
              : res;
          } else if (action === 'stop') {
            await stopAutoClear();
            result = { ok: true, state: getPublicClearState() };
          } else if (action === 'now') {
            const tab = await resolveClearTargetTab(null);
            if (!tab?.id || !isFlowProjectTab(tab)) {
              result = {
                ok: false,
                error: 'not_flow_project',
                message: 'Clear Data chỉ chạy trên tab Flow project (đường dẫn có /project/).',
              };
            } else {
              await clearProjectCookies(tab.id);
              clearState.clearCount = (clearState.clearCount || 0) + 1;
              await saveClearState();
              broadcastClearState();
              result = { ok: true, state: getPublicClearState() };
            }
          } else {
            result = { ok: false, error: 'invalid_action' };
          }
          sendToAgent({
            id: msg.id,
            status: result.ok ? 200 : 400,
            result,
            error: result.ok ? undefined : (result.error || result.message),
          });
        } catch (e) {
          sendToAgent({
            id: msg.id,
            status: 500,
            error: e?.message || 'clear_control_failed',
          });
        }
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
  const { url, method, headers, body, captchaAction } = params || {};

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
  const hasCaptcha = !!captchaAction;
  if (hasCaptcha) metrics.requestCount++;

  addRequestLog({
    id,
    type:   classifyUrl(url),
    time:   new Date().toISOString(),
    status: 'processing',
    url,
  });

  try {
    // Step 0: Fail fast if we have no bearer token. Avoids burning a reCAPTCHA
    // solve (rate-limited + single-use) only to discover later that we can't
    // send the request.
    if (!flowKey) {
      sendToAgent({ id, status: 503, error: 'NO_FLOW_KEY' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'NO_FLOW_KEY'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'NO_FLOW_KEY' });
      setState('idle');
      return;
    }

    // Step 1: Proactively refresh stale Flow token before burning captcha.
    const freshEnough = await ensureFreshFlowToken('api_request');
    if (!freshEnough || !flowKey) {
      sendToAgent({ id, status: 503, error: 'TOKEN_REFRESH_FAILED' });
      if (hasCaptcha) { metrics.failedCount++; metrics.lastError = 'TOKEN_REFRESH_FAILED'; }
      chrome.storage.local.set({ metrics });
      updateRequestLog(id, { status: 'failed', error: 'TOKEN_REFRESH_FAILED' });
      setState('idle');
      return;
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

    // Step 2: Solve captcha if needed
    let captchaToken = null;
    if (captchaAction) {
      const pidFromBody = extractProjectIdFromBody(body);
      if (pidFromBody) noteProjectId(pidFromBody);
      const captchaResult = await solveCaptcha(id, captchaAction);
      captchaToken = captchaResult?.token || null;
      if (captchaToken) {
        console.log(`[Flow2API] reCAPTCHA solved action=${captchaAction} len=${captchaToken.length}`);
      }
      if (!captchaToken) {
        const err = captchaResult?.error || 'CAPTCHA_FAILED';
        console.error(`[Flow2API] Captcha failed for ${captchaAction}: ${err}`);
        sendToAgent({ id, status: 403, error: `CAPTCHA_FAILED: ${err}` });
        if (hasCaptcha) { metrics.failedCount++; metrics.lastError = `CAPTCHA_FAILED: ${err}`; }
        chrome.storage.local.set({ metrics });
        updateRequestLog(id, { status: 'failed', error: `CAPTCHA_FAILED: ${err}` });
        setState('idle');
        return;
      }
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

    const fetchHeaders = { ...(headers || {}), authorization: `Bearer ${flowKey}` };
    let requestBody;
    if (method !== 'GET') {
      const isPlainObject = finalBody && typeof finalBody === 'object' && !(finalBody instanceof FormData) && !(finalBody instanceof Blob) && !(finalBody instanceof ArrayBuffer);
      requestBody = isPlainObject ? JSON.stringify(finalBody) : finalBody;
      if (isPlainObject && !Object.keys(fetchHeaders).some(k => k.toLowerCase() === 'content-type')) {
        fetchHeaders['Content-Type'] = 'application/json';
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

    if (response.status === 401) {
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
 */
async function openFlowTabResilient(active = false) {
  try {
    return await chrome.tabs.create({ url: FLOW_URL, active });
  } catch (e) {
    const msg = e?.message || '';
    if (!msg.includes('No current window')) throw e;
    console.log('[Flow2API] No Chrome window Ă¢â‚¬â€ spawning a fresh one for Flow');
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

async function ensureFreshFlowToken(reason = 'request', force = false) {
  const beforeToken = flowKey;
  const beforeCapturedAt = metrics.tokenCapturedAt || 0;
  const age = beforeCapturedAt ? Date.now() - beforeCapturedAt : Infinity;
  if (!force && flowKey && age < TOKEN_SOFT_MAX_AGE_MS) return true;

  const triggered = await captureTokenFromFlowTab();
  if (!triggered) return !!flowKey;

  const deadline = Date.now() + TOKEN_REFRESH_WAIT_MS;
  while (Date.now() < deadline) {
    if (flowKey && (flowKey !== beforeToken || (metrics.tokenCapturedAt || 0) > beforeCapturedAt)) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'token_captured', flowKey }));
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
      || host.endsWith('.googleapis.com')
      || host.endsWith('.googleusercontent.com');
  } catch {
    return false;
  }
}

async function handleTrpcRequest(msg) {
  const { id, params } = msg;
  const { url, method = 'POST', headers = {}, body } = params;

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

  const fetchHeaders = { ...headers };
  if (flowKey) {
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
    });
    const text = await resp.text();
    let data;
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = text;
    }
    sendToAgent({ id, status: resp.status, data });
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
  if (!flowKey) {
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

// ─── Auto Clear Site Data (current tab) ───────────────────────────────────────

const CLEAR_ALARM = 'f2api-auto-clear';
const AUTO_CLEAR_INTERVAL_SEC = 5;
const CLEAR_DATA_ORIGINS = ['https://labs.google'];

const DEFAULT_CLEAR_STATE = {
  running: false,
  intervalSec: AUTO_CLEAR_INTERVAL_SEC,
  tabId: null,
  origin: null,
  clearCount: 0,
  nextRunAt: null,
};

let clearState = { ...DEFAULT_CLEAR_STATE };
let _autoClearBootstrapping = false;

const CLEAR_COOKIE_OPTS = { cookies: true };

function clampClearSec(sec) {
  return Math.max(1, Math.min(3600, Math.round(Number(sec) || AUTO_CLEAR_INTERVAL_SEC)));
}

function canReloadTab(tab) {
  if (!tab?.id) return false;
  const url = tab.url || '';
  return url.startsWith('http://') || url.startsWith('https://') || url.startsWith('file://');
}

function getOriginFromUrl(url) {
  return new URL(url).origin;
}

function isLabsGoogleUrl(url) {
  try {
    return new URL(String(url || '')).hostname === 'labs.google';
  } catch {
    return false;
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

function isLabsGoogleTab(tab) {
  return !!tab && isLabsGoogleUrl(tab.url);
}

function isFlowProjectTab(tab) {
  return !!tab && isFlowProjectUrl(tab.url);
}

async function resolveClearTargetTab(tabId) {
  if (tabId) {
    try {
      const tab = await chrome.tabs.get(tabId);
      if (isFlowProjectTab(tab) && canReloadTab(tab)) return tab;
    } catch {
      /* fall through */
    }
  }
  return findFlowTabForClear();
}

function browsingDataRemove(origins, options) {
  return new Promise((resolve, reject) => {
    chrome.browsingData.remove({ origins }, options, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve();
      }
    });
  });
}

async function getActiveTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]?.id) return tabs[0];
  tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0] || null;
}

async function resolveTargetTab(tabId) {
  if (tabId) {
    try {
      return await chrome.tabs.get(tabId);
    } catch {
      return null;
    }
  }
  return getActiveTab();
}

async function findFlowTabForClear() {
  const tabs = await chrome.tabs.query({ url: flowUrls });
  return (
    tabs.find((t) => !t.discarded && canReloadTab(t) && isFlowProjectUrl(t.url))
    || tabs.find((t) => canReloadTab(t) && isFlowProjectUrl(t.url))
    || null
  );
}

async function ensureAutoClearStarted() {
  if (_autoClearBootstrapping) return;
  const prefs = await chrome.storage.local.get('f2apiClearUserStopped');
  if (prefs.f2apiClearUserStopped) return;
  _autoClearBootstrapping = true;
  try {
    await loadClearState();
    if (clearState.running && clearState.tabId) {
      try {
        const tab = await chrome.tabs.get(clearState.tabId);
        if (canReloadTab(tab) && isFlowProjectTab(tab)) {
          await scheduleClearAlarm();
          await saveClearState();
          broadcastClearState();
          return;
        }
      } catch {
        /* tab closed — start again below */
      }
    }

    const tab = await findFlowTabForClear();
    if (!tab?.id || !canReloadTab(tab)) return;

    const res = await startAutoClear(AUTO_CLEAR_INTERVAL_SEC, tab.id);
    if (res.ok) {
      console.log(
        '[Flow2API] Auto clear started: every %ss on %s',
        AUTO_CLEAR_INTERVAL_SEC,
        clearState.origin || tab.url,
      );
    } else {
      console.warn('[Flow2API] Auto clear start failed:', res.error || res.message || res);
    }
  } finally {
    _autoClearBootstrapping = false;
  }
}

async function loadClearState() {
  const data = await chrome.storage.local.get('f2apiClear');
  if (data.f2apiClear) {
    clearState = { ...DEFAULT_CLEAR_STATE, ...data.f2apiClear };
  }
}

async function saveClearState() {
  await chrome.storage.local.set({ f2apiClear: clearState });
}

function getPublicClearState() {
  let secondsUntilNext = null;
  if (clearState.running && clearState.nextRunAt) {
    secondsUntilNext = Math.max(0, Math.ceil((clearState.nextRunAt - Date.now()) / 1000));
  }
  return {
    running: clearState.running,
    intervalSec: clearState.intervalSec,
    clearCount: clearState.clearCount,
    tabId: clearState.tabId,
    origin: clearState.origin,
    cachedProjectId: _cachedProjectId,
    secondsUntilNext,
  };
}

function broadcastClearState() {
  chrome.runtime.sendMessage({
    type: 'CLEAR_STATE_UPDATE',
    state: getPublicClearState(),
  }).catch(() => {});
}

function alarmClear(name) {
  return new Promise((resolve) => chrome.alarms.clear(name, () => resolve()));
}

function alarmGet(name) {
  return new Promise((resolve) => chrome.alarms.get(name, (a) => resolve(a)));
}

function alarmCreate(name, info) {
  return new Promise((resolve, reject) => {
    chrome.alarms.create(name, info, () => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
      } else {
        resolve();
      }
    });
  });
}

async function scheduleClearAlarm() {
  await alarmClear(CLEAR_ALARM);
  if (!clearState.running || !clearState.tabId) return;

  const delayMin = Math.max(0.0167, clearState.intervalSec / 60);
  await alarmCreate(CLEAR_ALARM, { delayInMinutes: delayMin });
  clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
}

async function sanitizeClearState() {
  await loadClearState();
  if (!clearState.running) return;

  if (!clearState.tabId) {
    await stopAutoClear(false);
    return;
  }

  try {
    const tab = await chrome.tabs.get(clearState.tabId);
    if (!canReloadTab(tab) || !isFlowProjectTab(tab)) {
      await stopAutoClear(false);
      return;
    }
    clearState.origin = CLEAR_DATA_ORIGINS.join(', ');
  } catch {
    await stopAutoClear(false);
    return;
  }

  const alarm = await alarmGet(CLEAR_ALARM);
  if (!alarm) {
    clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
    await scheduleClearAlarm();
    await saveClearState();
  }
}

async function clearProjectCookies(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!canReloadTab(tab)) throw new Error('invalid_tab');
  if (!isFlowProjectUrl(tab.url)) throw new Error('not_flow_project');

  await browsingDataRemove(CLEAR_DATA_ORIGINS, CLEAR_COOKIE_OPTS);
  return CLEAR_DATA_ORIGINS.join(', ');
}

async function performClearTick() {
  if (!clearState.running || !clearState.tabId) return;

  try {
    const tab = await chrome.tabs.get(clearState.tabId);
    if (!isFlowProjectUrl(tab.url)) {
      await stopAutoClear(false);
      return;
    }
    clearState.origin = await clearProjectCookies(clearState.tabId);
    clearState.clearCount += 1;
    await scheduleClearAlarm();
    await saveClearState();
    broadcastClearState();
  } catch {
    await stopAutoClear();
  }
}

async function stopAutoClear(userStopped = true) {
  clearState.running = false;
  clearState.nextRunAt = null;
  await alarmClear(CLEAR_ALARM);
  await saveClearState();
  if (userStopped) {
    await chrome.storage.local.set({ f2apiClearUserStopped: true });
  }
  broadcastClearState();
}

async function startAutoClear(intervalSec, tabId) {
  const tab = await resolveClearTargetTab(tabId);
  if (!tab?.id || !canReloadTab(tab) || !isFlowProjectTab(tab)) {
    return {
      ok: false,
      error: 'not_flow_project',
      message: 'Clear Data chỉ chạy trên tab Flow project (đường dẫn có /project/). Mở project trước.',
      url: tab?.url || '',
    };
  }

  clearState.intervalSec = clampClearSec(intervalSec);
  clearState.tabId = tab.id;
  clearState.origin = CLEAR_DATA_ORIGINS.join(', ');
  clearState.running = true;
  await chrome.storage.local.set({ f2apiClearUserStopped: false });

  try {
    await clearProjectCookies(clearState.tabId);
    clearState.clearCount = (clearState.clearCount || 0) + 1;
    await scheduleClearAlarm();
    await saveClearState();
    broadcastClearState();
    return { ok: true, tabId: tab.id, origin: clearState.origin };
  } catch (err) {
    clearState.running = false;
    clearState.nextRunAt = null;
    clearState.tabId = null;
    clearState.origin = null;
    await alarmClear(CLEAR_ALARM);
    await saveClearState();
    broadcastClearState();
    return { ok: false, error: 'start_failed', message: String(err?.message || err) };
  }
}

async function initClearFromStorage() {
  await sanitizeClearState();
  if (clearState.running && clearState.tabId) {
    try {
      if (!clearState.nextRunAt || clearState.nextRunAt < Date.now()) {
        clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
      }
      await scheduleClearAlarm();
      broadcastClearState();
    } catch {
      await stopAutoClear();
    }
  }
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (clearState.tabId === tabId) stopAutoClear(false);
});

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
          // User-initiated Ă¢â€ â€™ focus the new window so they can see it.
          const tab = await openFlowTabResilient(true);
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

  if (msg.type === 'GET_AUTO_CLICK_CREATE') {
    chrome.storage.local.get(['autoClickCreateFlow', 'autoClickLastStatus']).then((data) => {
      reply({
        enabled: data.autoClickCreateFlow !== false,
        last: data.autoClickLastStatus || null,
      });
    });
    return true;
  }

  if (msg.type === 'AUTO_CLICK_STATUS') {
    const payload = {
      status: msg.status || '',
      message: msg.message || '',
      at: Date.now(),
    };
    chrome.storage.local.set({ autoClickLastStatus: payload }).then(() => {
      if (msg.status === 'success') {
        chrome.action.setBadgeText({ text: '✓' });
        chrome.action.setBadgeBackgroundColor({ color: '#16a34a' });
        setTimeout(() => chrome.action.setBadgeText({ text: '' }), 8000);
      }
    });
    return false;
  }

  if (msg.type === 'RETRY_AUTO_CLICK_CREATE') {
    chrome.tabs.query({
      url: ['https://labs.google/fx/tools/flow*', 'https://labs.google/fx/*/tools/flow*', 'https://labs.google/fx', 'https://labs.google/fx/*'],
    }).then((tabs) => {
      const tab = tabs.find((t) => !t.discarded) || tabs[0];
      if (!tab?.id) {
        reply?.({ ok: false, error: 'no_flow_tab' });
        return;
      }
      chrome.tabs.sendMessage(tab.id, { type: 'RETRY_AUTO_CLICK_CREATE' }).then(() => {
        reply?.({ ok: true });
      }).catch((e) => {
        reply?.({ ok: false, error: e?.message || 'send_failed' });
      });
    });
    return true;
  }

  if (msg.type === 'SET_AUTO_CLICK_CREATE') {
    const enabled = msg.enabled !== false;
    chrome.storage.local.set({ autoClickCreateFlow: enabled }).then(() => {
      reply({ ok: true, enabled });
    });
    return true;
  }

  if (msg.type === 'GET_CLEAR_STATE') {
    loadClearState().then(() => reply(getPublicClearState()));
    return true;
  }

  if (msg.type === 'SET_CLEAR_INTERVAL') {
    clearState.intervalSec = clampClearSec(msg.intervalSec);
    if (clearState.running) {
      clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
      scheduleClearAlarm();
    }
    saveClearState().then(() => reply({ ok: true }));
    return true;
  }

  if (msg.type === 'START_AUTO_CLEAR') {
    chrome.storage.local.set({ f2apiClearUserStopped: false }).then(() => {
      startAutoClear(msg.intervalSec, msg.tabId)
        .then((res) => reply(res))
        .catch((e) => reply({ ok: false, error: 'start_failed', message: e?.message || String(e) }));
    });
    return true;
  }

  if (msg.type === 'STOP_AUTO_CLEAR') {
    stopAutoClear().then(() => reply({ ok: true }));
    return true;
  }

  if (msg.type === 'RESET_CLEAR_COUNT') {
    clearState.clearCount = 0;
    saveClearState().then(() => {
      broadcastClearState();
      reply({ ok: true });
    });
    return true;
  }

  return true;
});

console.log('[Flow2API] Extension loaded');



