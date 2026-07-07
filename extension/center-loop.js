/**
 * Captcha Center loop — chạy khi mode = 'center'.
 *
 * Port từ veo3-captcha-extension với:
 * - Long-poll agent Python: GET /api/internal/captcha/poll
 * - Xử lý get_captcha / soft_reset / hard_reset
 * - Xoá _GRECAPTCHA anchor cookie (unpartitioned + CHIPS + browsingData)
 * - Sau hard_reset: about:blank → reload Flow → wait complete → dwell 2500ms
 * - Keepalive 24s (giữ SW MV3 sống)
 * - Exponential backoff khi poll fail: 500·2^N ms, max 10 000
 *
 * Không dùng globals của bridge (WS/flowKey/proxy). Chạy độc lập.
 */
'use strict';

// ── Constants (parity với veo3) ────────────────────────────────────────
const CENTER_DEFAULT_BASE = 'http://127.0.0.1:1994';
const CENTER_POLL_TIMEOUT_MS = 25_000;
const CENTER_FLOW_URL = 'https://labs.google/fx/tools/flow';
const CENTER_KEEPALIVE_MIN = 0.4; // ~24s alarm interval
const CENTER_HARD_RESET_BLANK_MS = 1500;
const CENTER_HARD_RESET_DWELL_MS = 2500;
const CENTER_CAPTCHA_TIMEOUT_MS = 25_000;
const CENTER_CAPTCHA_RETRY_TIMEOUT_MS = 20_000;

const CENTER_ANCHOR_NAME_RE = /grecaptcha/i;
const CENTER_ANCHOR_TOP_LEVEL_SITES = [
  'https://labs.google',
  'https://www.google.com',
  'https://google.com',
];

const CENTER_FLOW_URLS = [
  'https://labs.google/fx/tools/flow*',
  'https://labs.google/fx/*/tools/flow*',
];

const CENTER_EXTENSION_VERSION = chrome.runtime.getManifest().version;
const CENTER_ALARM_KEEPALIVE = 'f2api-center-keepalive';

// ── State ──────────────────────────────────────────────────────────────
let centerConsecutivePollErrors = 0;
let centerBridgeBase = CENTER_DEFAULT_BASE;
let centerBridgeSecret = '';
let centerId = '';
let centerLabel = '';
let centerConfigMissing = true;
let centerIsPolling = false;
let centerStopFlag = false;
let centerAbortPoll = null;

// ── Config ─────────────────────────────────────────────────────────────

async function centerLoadConfig() {
  const stored = await chrome.storage.local.get([
    'centerBridgeBase',
    'centerBridgeSecret',
    'centerId',
    'centerLabel',
  ]);
  centerBridgeBase = String(stored.centerBridgeBase || CENTER_DEFAULT_BASE).replace(/\/+$/, '');
  centerBridgeSecret = String(stored.centerBridgeSecret || '');
  centerId = String(stored.centerId || '');
  centerLabel = String(stored.centerLabel || '');
  if (!centerId) {
    centerId = crypto?.randomUUID?.() || `c-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    await chrome.storage.local.set({ centerId });
  }
  centerConfigMissing = !centerBridgeBase || !centerBridgeSecret;

  // Nếu secret chưa có, thử auto-fetch từ agent loopback (không cần user paste).
  if (!centerBridgeSecret) {
    try {
      const resp = await fetch(`${centerBridgeBase}/api/internal/captcha/secret`, {
        method: 'GET',
      });
      if (resp.ok) {
        const data = await resp.json();
        if (data?.secret) {
          centerBridgeSecret = String(data.secret);
          await chrome.storage.local.set({ centerBridgeSecret });
          centerConfigMissing = false;
          console.log('[Center] Auto-loaded secret from agent loopback');
        }
      }
    } catch (e) {
      /* offline → user tự set qua popup */
    }
  }
  centerUpdateBadge();
}

function centerUpdateBadge() {
  if (centerConfigMissing) {
    chrome.action.setBadgeText({ text: '!' });
    chrome.action.setBadgeBackgroundColor({ color: '#dc2626' });
    chrome.action.setTitle({ title: 'Captcha Center — cần cấu hình bridge secret' });
  } else {
    chrome.action.setBadgeText({ text: 'C' });
    chrome.action.setBadgeBackgroundColor({ color: '#0ea5e9' });
    chrome.action.setTitle({ title: `Captcha Center ${centerLabel || centerId.slice(0, 8)}` });
  }
}

// ── HTTP helpers ───────────────────────────────────────────────────────

function centerAuthHeaders() {
  return centerBridgeSecret ? { 'x-center-secret': centerBridgeSecret } : {};
}

async function centerPostJson(path, payload) {
  try {
    await fetch(`${centerBridgeBase}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...centerAuthHeaders() },
      body: JSON.stringify({ centerId, ...payload }),
    });
  } catch (e) {
    console.warn('[Center] post failed:', path, e?.message || e);
  }
}

// ── Tab helpers ────────────────────────────────────────────────────────

function centerIsFlowUrl(url) {
  if (!url) return false;
  return (
    /\/fx\/tools\/flow/i.test(url) ||
    /\/fx\/[^/]+\/tools\/flow/i.test(url)
  );
}

async function centerPinFlowTab(tab) {
  if (tab?.id) {
    await chrome.storage.local.set({ centerFlowTabId: tab.id });
  }
}

async function centerFindFlowTab() {
  const stored = await chrome.storage.local.get(['centerFlowTabId']);
  const preferredId = stored.centerFlowTabId;
  if (preferredId) {
    try {
      const tab = await chrome.tabs.get(preferredId);
      if (tab?.id && centerIsFlowUrl(tab.url)) return tab;
    } catch (_) {
      /* tab đã đóng — chọn lại */
    }
  }

  const tabs = await chrome.tabs.query({ url: CENTER_FLOW_URLS });
  if (tabs.length > 1) {
    console.warn(
      `[Center] ${tabs.length} tab Flow — dùng tab ${tabs[0].id}. ` +
        'Nên chỉ giữ 1 tab Flow trên mỗi profile Center.',
    );
  }
  const tab = tabs.length > 0 ? tabs[0] : null;
  if (tab) await centerPinFlowTab(tab);
  return tab;
}

async function centerEnsureContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
  } catch (_) {
    /* already injected */
  }
}

function centerWaitForTabLoad(tabId, timeoutMs = 30_000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
      reject(new Error('waitForTabLoad: timeout'));
    }, timeoutMs);

    function onUpdated(id, info) {
      if (id !== tabId) return;
      if (info.status === 'complete') {
        clearTimeout(timer);
        chrome.tabs.onUpdated.removeListener(onUpdated);
        chrome.tabs.onRemoved.removeListener(onRemoved);
        resolve();
      }
    }
    function onRemoved(id) {
      if (id !== tabId) return;
      clearTimeout(timer);
      chrome.tabs.onUpdated.removeListener(onUpdated);
      chrome.tabs.onRemoved.removeListener(onRemoved);
      reject(new Error('waitForTabLoad: tab removed'));
    }

    chrome.tabs.onUpdated.addListener(onUpdated);
    chrome.tabs.onRemoved.addListener(onRemoved);
  });
}

// ── Anchor cookie clear (3 passes: unpartitioned + CHIPS + browsingData) ─

async function centerDumpAnchorCookies() {
  try {
    const all = await chrome.cookies.getAll({});
    return all
      .filter((c) => CENTER_ANCHOR_NAME_RE.test(c.name))
      .map(
        (c) =>
          `${c.name}@${c.domain}${c.partitionKey ? ` [part:${JSON.stringify(c.partitionKey)}]` : ''}`,
      );
  } catch (_) {
    return [];
  }
}

async function centerRemoveAnchorCookies(query, extra) {
  let removed = 0;
  const cookies = await chrome.cookies.getAll(query);
  for (const cookie of cookies) {
    if (!CENTER_ANCHOR_NAME_RE.test(cookie.name)) continue;
    const host = cookie.domain.startsWith('.') ? `www${cookie.domain}` : cookie.domain;
    const url = `${cookie.secure ? 'https' : 'http'}://${host}${cookie.path}`;
    try {
      const detail = await chrome.cookies.remove({
        url,
        name: cookie.name,
        storeId: cookie.storeId,
        ...extra,
      });
      if (detail !== null) removed++;
    } catch (_) {
      /* individual remove errors ignored */
    }
  }
  return removed;
}

async function centerClearAnchor() {
  const before = await centerDumpAnchorCookies();
  let removed = 0;
  let partitionedRemoved = 0;
  try {
    removed = await centerRemoveAnchorCookies({});
  } catch (e) {
    console.warn('[Center] anchor clear (unpartitioned) failed:', e?.message || e);
  }
  for (const topLevelSite of CENTER_ANCHOR_TOP_LEVEL_SITES) {
    try {
      const partitionKey = { topLevelSite };
      partitionedRemoved += await centerRemoveAnchorCookies({ partitionKey }, { partitionKey });
    } catch (_e) {
      /* older Chrome — CHIPS unsupported */
    }
  }
  if (chrome.browsingData?.remove) {
    try {
      await chrome.browsingData.remove(
        { origins: ['https://www.google.com', 'https://www.recaptcha.net'] },
        { cookies: true },
      );
    } catch (e) {
      console.warn('[Center] browsingData net failed:', e?.message || e);
    }
  }
  const after = await centerDumpAnchorCookies();
  console.log(
    `[Center] anchor clear: unpartitioned=${removed} chips=${partitionedRemoved} before=${before.length} after=${after.length}`,
  );
  void centerPostJson('/api/internal/captcha/event', {
    centerId,
    type: 'anchor_clear',
    version: CENTER_EXTENSION_VERSION,
    payload: {
      removed,
      partitionedRemoved,
      before: before.length,
      after: after.length,
      sample: before.slice(0, 8),
    },
  });
  return removed + partitionedRemoved;
}

// ── Command handlers ───────────────────────────────────────────────────

async function centerHandleCaptcha(command) {
  const { commandId, action } = command;
  const startedAt = Date.now();

  const tab = await centerFindFlowTab();
  if (!tab?.id) {
    await centerPostJson('/api/internal/captcha/result', { commandId, error: 'NO_FLOW_TAB' });
    return;
  }

  const ask = (timeoutMs) =>
    new Promise((resolve) => {
      const timer = setTimeout(() => resolve({ error: 'CAPTCHA_TIMEOUT' }), timeoutMs);
      chrome.tabs.sendMessage(
        tab.id,
        { type: 'GET_CAPTCHA', requestId: commandId, pageAction: action },
        (resp) => {
          clearTimeout(timer);
          if (chrome.runtime.lastError) {
            resolve({ error: chrome.runtime.lastError.message || 'TAB_NO_LISTENER' });
            return;
          }
          resolve(resp || { error: 'EMPTY_REPLY' });
        },
      );
    });

  let reply = await ask(CENTER_CAPTCHA_TIMEOUT_MS);
  if (!reply?.token) {
    await centerEnsureContentScript(tab.id);
    await new Promise((r) => setTimeout(r, 200));
    reply = await ask(CENTER_CAPTCHA_RETRY_TIMEOUT_MS);
  }

  const durationMs = Date.now() - startedAt;
  if (reply?.token) {
    void chrome.storage.session.set({
      centerLastMintOkAt: Date.now(),
      centerLastMintDurationMs: durationMs,
    });
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      token: reply.token,
    });
  } else {
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      error: reply?.error || 'CAPTCHA_FAILED',
    });
  }
}

async function centerHandleSoftReset(command) {
  const { commandId } = command;
  const tab = await centerFindFlowTab();
  if (!tab?.id) {
    await centerPostJson('/api/internal/captcha/result', { commandId, error: 'NO_FLOW_TAB' });
    return;
  }
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      world: 'MAIN',
      func: () => {
        const keys = Object.keys(localStorage).filter((k) => k.startsWith('_grecaptcha'));
        keys.forEach((k) => localStorage.removeItem(k));
        return keys.length;
      },
    });
    await centerClearAnchor();
    await centerPostJson('/api/internal/captcha/event', {
      centerId,
      type: 'soft_reset_finished',
      version: CENTER_EXTENSION_VERSION,
    });
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      token: 'soft_reset_ok',
    });
  } catch (e) {
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      error: e?.message || 'SOFT_RESET_FAILED',
    });
  }
}

async function centerHandleHardReset(command) {
  const { commandId } = command;
  const tab = await centerFindFlowTab();
  if (!tab?.id) {
    await centerPostJson('/api/internal/captcha/result', { commandId, error: 'NO_FLOW_TAB' });
    return;
  }
  try {
    await centerPostJson('/api/internal/captcha/event', {
      centerId,
      type: 'hard_reset_started',
      version: CENTER_EXTENSION_VERSION,
    });
    await centerClearAnchor();
    await chrome.tabs.update(tab.id, { url: 'about:blank' });
    await new Promise((r) => setTimeout(r, CENTER_HARD_RESET_BLANK_MS));
    await chrome.tabs.update(tab.id, { url: CENTER_FLOW_URL });
    await centerWaitForTabLoad(tab.id, 30_000);
    await new Promise((r) => setTimeout(r, CENTER_HARD_RESET_DWELL_MS));
    await centerPostJson('/api/internal/captcha/event', {
      centerId,
      type: 'hard_reset_finished',
      version: CENTER_EXTENSION_VERSION,
    });
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      token: 'hard_reset_ok',
    });
  } catch (e) {
    await centerPostJson('/api/internal/captcha/event', {
      centerId,
      type: 'hard_reset_finished',
      version: CENTER_EXTENSION_VERSION,
    });
    await centerPostJson('/api/internal/captcha/result', {
      commandId,
      centerId,
      error: e?.message || 'HARD_RESET_FAILED',
    });
  }
}

// ── Poll loop ──────────────────────────────────────────────────────────

async function centerPollLoop() {
  centerIsPolling = true;
  while (!centerStopFlag) {
    if (centerConfigMissing) {
      console.warn('[Center] stopping — config missing');
      centerIsPolling = false;
      return;
    }

    let commands = [];
    const controller = new AbortController();
    centerAbortPoll = controller;
    try {
      const url =
        `${centerBridgeBase}/api/internal/captcha/poll?centerId=${encodeURIComponent(centerId)}` +
        `&label=${encodeURIComponent(centerLabel || '')}` +
        `&version=${encodeURIComponent(CENTER_EXTENSION_VERSION)}` +
        `&timeout=${Math.floor(CENTER_POLL_TIMEOUT_MS / 1000)}`;
      const resp = await fetch(url, {
        method: 'GET',
        headers: centerAuthHeaders(),
        signal: controller.signal,
      });
      if (resp.ok) {
        const data = await resp.json();
        commands = Array.isArray(data?.commands) ? data.commands : [];
        centerConsecutivePollErrors = 0;
        void chrome.storage.session.set({ centerLastPollOkAt: Date.now() });
      } else if (resp.status === 401) {
        console.error('[Center] poll HTTP 401 — sai secret');
        centerConfigMissing = true;
        centerUpdateBadge();
      } else {
        console.warn('[Center] poll HTTP', resp.status);
        centerConsecutivePollErrors += 1;
      }
    } catch (e) {
      if (e?.name !== 'AbortError') {
        centerConsecutivePollErrors += 1;
      }
    } finally {
      centerAbortPoll = null;
    }

    for (const cmd of commands) {
      if (cmd.method === 'get_captcha') void centerHandleCaptcha(cmd);
      else if (cmd.method === 'soft_reset') void centerHandleSoftReset(cmd);
      else if (cmd.method === 'hard_reset') void centerHandleHardReset(cmd);
      else console.warn('[Center] unknown command method:', cmd.method);
    }

    if (centerConsecutivePollErrors > 0) {
      const backoff = Math.min(
        10_000,
        500 * Math.pow(2, Math.min(centerConsecutivePollErrors, 5)),
      );
      await new Promise((r) => setTimeout(r, backoff));
    }
  }
  centerIsPolling = false;
}

// ── Flow tab helper ────────────────────────────────────────────────────

async function centerEnsureFlowTab() {
  const existing = await centerFindFlowTab();
  if (existing) return existing;
  try {
    const tab = await chrome.tabs.create({ url: CENTER_FLOW_URL, active: false });
    await centerPinFlowTab(tab);
    return tab;
  } catch (e) {
    // No window → spawn new window
    try {
      const win = await chrome.windows.create({
        url: CENTER_FLOW_URL,
        focused: false,
        state: 'minimized',
      });
      const tab = win.tabs?.[0] ?? null;
      await centerPinFlowTab(tab);
      return tab;
    } catch (e2) {
      console.warn('[Center] ensureFlowTab failed:', e2?.message || e2);
      return null;
    }
  }
}

// ── Bootstrap / lifecycle ──────────────────────────────────────────────

async function centerStart() {
  centerStopFlag = false;
  await centerLoadConfig();
  chrome.alarms.create(CENTER_ALARM_KEEPALIVE, { periodInMinutes: CENTER_KEEPALIVE_MIN });

  await centerEnsureFlowTab();

  if (centerConfigMissing) {
    console.warn('[Center] config missing — chờ user set secret qua popup');
    return;
  }
  await centerPostJson('/api/internal/captcha/event', {
    centerId,
    type: 'extension_ready',
    label: centerLabel,
    version: CENTER_EXTENSION_VERSION,
  });
  if (!centerIsPolling) void centerPollLoop();
}

function centerStop() {
  centerStopFlag = true;
  try {
    centerAbortPoll?.abort();
  } catch (_) {
    /* ignore */
  }
  chrome.alarms.clear(CENTER_ALARM_KEEPALIVE);
  chrome.action.setBadgeText({ text: '' });
  chrome.action.setTitle({ title: 'Flow2API Bridge' });
}

// ── Storage change hook ────────────────────────────────────────────────

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local') return;
  let refreshed = false;
  if (changes.centerBridgeBase && typeof changes.centerBridgeBase.newValue === 'string') {
    centerBridgeBase = String(changes.centerBridgeBase.newValue || CENTER_DEFAULT_BASE).replace(/\/+$/, '');
    refreshed = true;
  }
  if (changes.centerBridgeSecret && typeof changes.centerBridgeSecret.newValue === 'string') {
    centerBridgeSecret = String(changes.centerBridgeSecret.newValue || '');
    refreshed = true;
  }
  if (changes.centerLabel && typeof changes.centerLabel.newValue === 'string') {
    centerLabel = String(changes.centerLabel.newValue || '');
    refreshed = true;
  }
  if (!refreshed) return;
  const wasMissing = centerConfigMissing;
  centerConfigMissing = !centerBridgeBase || !centerBridgeSecret;
  centerUpdateBadge();
  if (wasMissing && !centerConfigMissing && !centerIsPolling) {
    void centerPollLoop();
  }
});

// ── Keepalive & heartbeat ──────────────────────────────────────────────

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== CENTER_ALARM_KEEPALIVE || centerStopFlag) return;
  if (centerConfigMissing) return;
  const tab = await centerFindFlowTab();
  void centerPostJson('/api/internal/captcha/event', {
    centerId,
    type: 'heartbeat',
    label: centerLabel,
    version: CENTER_EXTENSION_VERSION,
    payload: { hasFlowTab: !!tab },
  });
});

// Expose to background.js (importScripts scope) — không phải window/self default.
self.__centerLoop = {
  start: centerStart,
  stop: centerStop,
};
