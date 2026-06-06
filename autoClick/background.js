const DEFAULT_STATE = {
  points: [],
  textTargets: [],
  clickMode: "coords",
  interval: 1.0,
  humanize: true,
  running: false,
  capturing: false,
  capturingText: false,
  widgetVisible: false,
  showMarkers: true,
  maxCycles: 0,
  intervalStep: 0.1,
  clickSequence: true,
  stats: {
    cycles: 0,
    totalClicks: 0,
    elapsedMs: 0,
  },
  sessionStart: null,
};

let state = { ...DEFAULT_STATE };
let activeTabId = null;
let loopTimer = null;
let elapsedTimer = null;
let currentPointIndex = 0;
let cyclesDone = 0;

async function loadState() {
  const data = await chrome.storage.local.get("sacState");
  if (data.sacState) {
    state = { ...DEFAULT_STATE, ...data.sacState, stats: { ...DEFAULT_STATE.stats, ...data.sacState.stats } };
    normalizeAllPoints();
    normalizeAllTextTargets();
  }
}

async function saveState() {
  await chrome.storage.local.set({ sacState: state });
  flashSaved();
}

function flashSaved() {
  chrome.runtime.sendMessage({ type: "SAVED" }).catch(() => {});
}

async function getActiveTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]?.id) return tabs[0];
  tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

async function ensureContentScript(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "PING" });
    return true;
  } catch {
    try {
      await chrome.scripting.executeScript({
        target: { tabId },
        files: ["content.js"],
      });
      await chrome.scripting.insertCSS({
        target: { tabId },
        files: ["content.css"],
      });
      return true;
    } catch {
      return false;
    }
  }
}

async function syncToTab(tabId) {
  if (!tabId) return;
  const ok = await ensureContentScript(tabId);
  if (!ok) return;

  const widgetData = getWidgetData();

  chrome.tabs.sendMessage(tabId, {
    type: "SYNC_STATE",
    points: state.points,
    textTargets: state.textTargets,
    clickMode: state.clickMode,
    showMarkers: state.showMarkers,
    capturing: state.capturing,
    capturingText: state.capturingText,
    widgetVisible: state.widgetVisible,
    widgetData,
  }).catch(() => {});
}

function formatTime(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const sec = s % 60;
  return `${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`;
}

function getWidgetData() {
  return {
    running: state.running,
    totalClicks: state.stats.totalClicks,
    cycles: state.stats.cycles,
    targets: getClickSequence().length,
    elapsed: formatTime(getElapsedMs()),
  };
}

function clampDelay(sec) {
  return Math.max(0.1, Math.min(300, Number(sec) || state.interval));
}

function normalizePoint(pt) {
  return {
    x: pt.x,
    y: pt.y,
    delay: clampDelay(pt.delay ?? pt.interval ?? state.interval),
  };
}

function normalizeAllPoints() {
  state.points = state.points.map(normalizePoint);
}

function normalizeTextTarget(t) {
  const text = String(t.text || "").trim().slice(0, 200);
  return {
    text,
    delay: clampDelay(t.delay ?? state.interval),
    exact: t.exact !== false,
  };
}

function normalizeAllTextTargets() {
  state.textTargets = (state.textTargets || [])
    .map(normalizeTextTarget)
    .filter((t) => t.text.length > 0);
}

function getClickSequence() {
  const coords = state.points.map((p) => ({ kind: "coord", ...p }));
  const texts = (state.textTargets || []).map((t) => ({ kind: "text", ...t }));
  if (state.clickMode === "text") return texts;
  if (state.clickMode === "both") return [...coords, ...texts];
  return coords;
}

function getTargetDelay(target) {
  if (!target) return state.interval;
  return clampDelay(target.delay);
}

function humanizedDelay(baseSec) {
  const sec = clampDelay(baseSec);
  if (!state.humanize) return sec * 1000;
  const variance = 0.1;
  const factor = 1 + (Math.random() * 2 - 1) * variance;
  return Math.max(50, sec * factor * 1000);
}

function broadcastState() {
  chrome.runtime.sendMessage({
    type: "STATE_UPDATE",
    state: getPublicState(),
  }).catch(() => {});

  if (activeTabId) {
    syncToTab(activeTabId);
    if (state.widgetVisible) {
      chrome.tabs.sendMessage(activeTabId, {
        type: "WIDGET_UPDATE",
        data: getWidgetData(),
      }).catch(() => {});
    }
  }
}

function getPublicState() {
  const elapsed = getElapsedMs();
  const seq = getClickSequence();
  return {
    points: state.points,
    textTargets: state.textTargets,
    clickMode: state.clickMode || "coords",
    sequenceCount: seq.length,
    interval: state.interval,
    humanize: state.humanize,
    running: state.running,
    capturing: state.capturing,
    capturingText: state.capturingText,
    widgetVisible: state.widgetVisible,
    showMarkers: state.showMarkers,
    maxCycles: state.maxCycles,
    intervalStep: state.intervalStep,
    clickSequence: state.clickSequence,
    stats: { ...state.stats, elapsedMs: elapsed },
    elapsedFormatted: formatTime(elapsed),
  };
}

function getElapsedMs() {
  let ms = state.stats.elapsedMs;
  if (state.running && state._timerResumeAt) {
    ms += Date.now() - state._timerResumeAt;
  }
  return ms;
}

function startElapsedTimer() {
  stopElapsedTimer();
  elapsedTimer = setInterval(() => {
    if (!state.running) return;
    const pub = getPublicState();
    pub.stats.elapsedMs = getElapsedMs();
    pub.elapsedFormatted = formatTime(pub.stats.elapsedMs);
    chrome.runtime.sendMessage({ type: "STATE_UPDATE", state: pub }).catch(() => {});
    if (activeTabId && state.widgetVisible) {
      chrome.tabs.sendMessage(activeTabId, {
        type: "WIDGET_UPDATE",
        data: getWidgetData(),
      }).catch(() => {});
    }
  }, 500);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

function pauseRunning() {
  if (state.running && state._timerResumeAt) {
    state.stats.elapsedMs += Date.now() - state._timerResumeAt;
    state._timerResumeAt = null;
  }
  state.running = false;
  clearTimeout(loopTimer);
  loopTimer = null;
  stopElapsedTimer();
  broadcastState();
}

async function clickTextAt(tabId, text, exact) {
  try {
    const res = await chrome.tabs.sendMessage(tabId, {
      type: "PERFORM_TEXT_CLICK",
      text,
      exact: exact !== false,
    });
    return res?.ok;
  } catch {
    return false;
  }
}

async function startRunning() {
  if (!getClickSequence().length) return false;

  const tab = await getActiveTab();
  if (!tab?.id) return false;
  activeTabId = tab.id;

  state.running = true;
  state._timerResumeAt = Date.now();
  currentPointIndex = 0;
  startElapsedTimer();
  broadcastState();
  scheduleNextClick(0);
  return true;
}

async function clickAt(tabId, x, y) {
  try {
    const res = await chrome.tabs.sendMessage(tabId, { type: "PERFORM_CLICK", x, y });
    return res?.ok;
  } catch {
    return false;
  }
}

function scheduleNextClick(waitSec = 0) {
  if (!state.running) return;

  clearTimeout(loopTimer);
  loopTimer = setTimeout(async () => {
    if (!state.running || !activeTabId) return;

    const clickedIndex = currentPointIndex;
    const sequence = getClickSequence();
    const target = sequence[clickedIndex];
    if (target) {
      let ok = false;
      if (target.kind === "text") {
        ok = await clickTextAt(activeTabId, target.text, target.exact);
      } else {
        ok = await clickAt(activeTabId, target.x, target.y);
      }
      if (ok) state.stats.totalClicks += 1;
    }

    const delayAfter = getTargetDelay(target);

    currentPointIndex += 1;

    if (currentPointIndex >= sequence.length) {
      currentPointIndex = 0;
      cyclesDone += 1;
      state.stats.cycles = cyclesDone;

      if (state.maxCycles > 0 && cyclesDone >= state.maxCycles) {
        pauseRunning();
        saveState();
        return;
      }
    }

    saveState();
    broadcastState();
    scheduleNextClick(delayAfter);
  }, humanizedDelay(waitSec));
}

async function toggleRunning() {
  if (state.running) {
    pauseRunning();
    await saveState();
    return;
  }
  cyclesDone = state.stats.cycles;
  const ok = await startRunning();
  if (ok) await saveState();
}

async function setCapture(enabled) {
  state.capturing = enabled;
  if (enabled) state.capturingText = false;
  const tab = await getActiveTab();
  if (tab?.id) {
    activeTabId = tab.id;
    await syncToTab(tab.id);
    await chrome.tabs.sendMessage(tab.id, {
      type: "SET_CAPTURE",
      enabled,
      capturingText: false,
    }).catch(() => {});
  }
  broadcastState();
  await saveState();
}

async function setTextCapture(enabled) {
  state.capturingText = enabled;
  if (enabled) state.capturing = false;
  const tab = await getActiveTab();
  if (tab?.id) {
    activeTabId = tab.id;
    await syncToTab(tab.id);
    await chrome.tabs.sendMessage(tab.id, {
      type: "SET_TEXT_CAPTURE",
      enabled,
      capturing: false,
    }).catch(() => {});
  }
  broadcastState();
  await saveState();
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "GET_STATE":
        sendResponse(getPublicState());
        break;

      case "CONTENT_READY": {
        const tabId = sender.tab?.id;
        if (tabId) {
          activeTabId = tabId;
          await syncToTab(tabId);
        }
        sendResponse({ ok: true });
        break;
      }

      case "POINT_ADDED":
        state.points.push(normalizePoint(msg.point));
        await saveState();
        broadcastState();
        sendResponse({ ok: true, points: state.points });
        break;

      case "CAPTURE_STOPPED":
        state.capturing = false;
        state.capturingText = false;
        await saveState();
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "TEXT_TARGET_ADDED": {
        const entry = normalizeTextTarget(msg.target || { text: msg.text });
        if (entry.text) {
          const dup = state.textTargets.some((t) => t.text === entry.text);
          if (!dup) state.textTargets.push(entry);
          await saveState();
          broadcastState();
        }
        sendResponse({ ok: true, textTargets: state.textTargets });
        break;
      }

      case "SET_TEXT_CAPTURE":
        await setTextCapture(msg.enabled);
        sendResponse({ ok: true });
        break;

      case "SET_CLICK_MODE":
        state.clickMode = ["coords", "text", "both"].includes(msg.mode) ? msg.mode : "coords";
        await saveState();
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "SET_TEXT_TARGET_DELAY": {
        const idx = msg.index;
        if (idx >= 0 && idx < state.textTargets.length) {
          state.textTargets[idx].delay = clampDelay(msg.delay);
          await saveState();
          broadcastState();
        }
        sendResponse({ ok: true });
        break;
      }

      case "REMOVE_TEXT_TARGET":
        state.textTargets.splice(msg.index, 1);
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, {
            type: "SET_TEXT_TARGETS",
            textTargets: state.textTargets,
          }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "CLEAR_TEXT_TARGETS":
        state.textTargets = [];
        pauseRunning();
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, {
            type: "SET_TEXT_TARGETS",
            textTargets: [],
          }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "APPLY_DEFAULT_TEXT_DELAY":
        state.textTargets = state.textTargets.map((t) => ({ ...t, delay: state.interval }));
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, {
            type: "SET_TEXT_TARGETS",
            textTargets: state.textTargets,
          }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "SET_CAPTURE":
        await setCapture(msg.enabled);
        sendResponse({ ok: true });
        break;

      case "TOGGLE_RUNNING":
        await toggleRunning();
        sendResponse({ ok: true, state: getPublicState() });
        break;

      case "TOGGLE_FROM_WIDGET":
        await toggleRunning();
        sendResponse({ ok: true });
        break;

      case "SET_INTERVAL":
        state.interval = clampDelay(msg.interval);
        if (msg.applyToAll) {
          state.points = state.points.map((pt) => ({ ...pt, delay: state.interval }));
        }
        await saveState();
        if (activeTabId && msg.applyToAll) {
          await chrome.tabs.sendMessage(activeTabId, { type: "SET_POINTS", points: state.points }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "SET_POINT_DELAY": {
        const idx = msg.index;
        if (idx >= 0 && idx < state.points.length) {
          state.points[idx].delay = clampDelay(msg.delay);
          await saveState();
          broadcastState();
        }
        sendResponse({ ok: true });
        break;
      }

      case "APPLY_DEFAULT_DELAY":
        state.points = state.points.map((pt) => ({ ...pt, delay: state.interval }));
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, { type: "SET_POINTS", points: state.points }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "SET_HUMANIZE":
        state.humanize = !!msg.enabled;
        await saveState();
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "REMOVE_POINT":
        state.points.splice(msg.index, 1);
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, { type: "SET_POINTS", points: state.points }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "CLEAR_POINTS":
        state.points = [];
        pauseRunning();
        await saveState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, { type: "SET_POINTS", points: [] }).catch(() => {});
        }
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "RESET_STATS":
        state.stats = { cycles: 0, totalClicks: 0, elapsedMs: 0 };
        state.sessionStart = null;
        cyclesDone = 0;
        currentPointIndex = 0;
        await saveState();
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "TOGGLE_WIDGET":
        state.widgetVisible = !state.widgetVisible;
        await saveState();
        broadcastState();
        if (activeTabId) {
          await chrome.tabs.sendMessage(activeTabId, {
            type: "SET_WIDGET",
            visible: state.widgetVisible,
            data: getWidgetData(),
          }).catch(() => {});
        }
        sendResponse({ ok: true });
        break;

      case "UPDATE_SETTINGS":
        Object.assign(state, {
          maxCycles: Math.max(0, parseInt(msg.maxCycles, 10) || 0),
          intervalStep: Math.max(0.05, parseFloat(msg.intervalStep) || 0.1),
          showMarkers: !!msg.showMarkers,
          clickSequence: msg.clickSequence !== false,
        });
        await saveState();
        broadcastState();
        sendResponse({ ok: true });
        break;

      case "GET_REFRESH_STATE":
        await sanitizeRefreshState();
        sendResponse(getPublicRefreshState());
        break;

      case "SET_REFRESH_INTERVAL":
        refreshState.intervalSec = clampRefreshSec(msg.intervalSec);
        if (refreshState.running) {
          refreshState.nextRefreshAt = Date.now() + refreshState.intervalSec * 1000;
          await scheduleRefreshAlarm();
        }
        await saveRefreshState();
        broadcastRefreshState();
        sendResponse({ ok: true });
        break;

      case "START_AUTO_REFRESH": {
        let result = { ok: false, error: "unknown" };
        try {
          if (refreshState.running) await stopAutoRefresh();
          result = await startAutoRefresh(
            msg.intervalSec ?? refreshState.intervalSec,
            msg.tabId
          );
        } catch (err) {
          result = { ok: false, error: "exception", message: String(err?.message || err) };
        }
        sendResponse(result);
        break;
      }

      case "STOP_AUTO_REFRESH":
        await stopAutoRefresh();
        sendResponse({ ok: true });
        break;

      case "RESET_REFRESH_COUNT":
        refreshState.refreshCount = 0;
        await saveRefreshState();
        broadcastRefreshState();
        sendResponse({ ok: true });
        break;

      case "GET_CLEAR_STATE":
        await sanitizeClearState();
        sendResponse(getPublicClearState());
        break;

      case "SET_CLEAR_INTERVAL":
        clearState.intervalSec = clampRefreshSec(msg.intervalSec);
        if (clearState.running) {
          clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
          await scheduleClearAlarm();
        }
        await saveClearState();
        broadcastClearState();
        sendResponse({ ok: true });
        break;

      case "START_AUTO_CLEAR": {
        let result = { ok: false, error: "unknown" };
        try {
          if (clearState.running) await stopAutoClear();
          result = await startAutoClear(
            msg.intervalSec ?? clearState.intervalSec,
            msg.tabId
          );
        } catch (err) {
          result = { ok: false, error: "exception", message: String(err?.message || err) };
        }
        sendResponse(result);
        break;
      }

      case "STOP_AUTO_CLEAR":
        await stopAutoClear();
        sendResponse({ ok: true });
        break;

      case "RESET_CLEAR_COUNT":
        clearState.clearCount = 0;
        await saveClearState();
        broadcastClearState();
        sendResponse({ ok: true });
        break;

      default:
        break;
    }
  })();
  return true;
});

chrome.commands.onCommand.addListener(async (command) => {
  await loadState();
  if (command === "add-points") {
    await setTextCapture(false);
    await setCapture(!state.capturing);
  } else if (command === "add-text") {
    await setCapture(false);
    await setTextCapture(!state.capturingText);
  } else if (command === "toggle-clicker") {
    await toggleRunning();
  }
});

chrome.tabs.onActivated.addListener(async () => {
  const tab = await getActiveTab();
  if (tab?.id) {
    activeTabId = tab.id;
    await syncToTab(tab.id);
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, info) => {
  if (info.status === "complete") {
    await syncToTab(tabId);
  }
});

// --- Auto Refresh ---
const REFRESH_ALARM = "sac-auto-refresh";

const DEFAULT_REFRESH_STATE = {
  running: false,
  intervalSec: 10,
  tabId: null,
  refreshCount: 0,
  nextRefreshAt: null,
};

let refreshState = { ...DEFAULT_REFRESH_STATE };

function canReloadTab(tab) {
  if (!tab?.id) return false;
  const url = tab.url || "";
  return (
    url.startsWith("http://") ||
    url.startsWith("https://") ||
    url.startsWith("file://")
  );
}

function clampRefreshSec(sec) {
  return Math.max(1, Math.min(3600, Math.round(Number(sec) || 10)));
}

function getPublicRefreshState() {
  let secondsUntilNext = null;
  if (refreshState.running && refreshState.nextRefreshAt) {
    secondsUntilNext = Math.max(0, Math.ceil((refreshState.nextRefreshAt - Date.now()) / 1000));
  }
  return {
    running: refreshState.running,
    intervalSec: refreshState.intervalSec,
    refreshCount: refreshState.refreshCount,
    tabId: refreshState.tabId,
    secondsUntilNext,
  };
}

function broadcastRefreshState() {
  chrome.runtime.sendMessage({
    type: "REFRESH_STATE_UPDATE",
    state: getPublicRefreshState(),
  }).catch(() => {});
}

async function loadRefreshState() {
  const data = await chrome.storage.local.get("sacRefresh");
  if (data.sacRefresh) {
    refreshState = { ...DEFAULT_REFRESH_STATE, ...data.sacRefresh };
  }
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

/** Fix stale state; re-create alarm if missing instead of stopping. */
async function sanitizeRefreshState() {
  await loadRefreshState();
  if (!refreshState.running) return;

  if (!refreshState.tabId) {
    await stopAutoRefresh();
    return;
  }

  try {
    const tab = await chrome.tabs.get(refreshState.tabId);
    if (!canReloadTab(tab)) {
      await stopAutoRefresh();
      return;
    }
  } catch {
    await stopAutoRefresh();
    return;
  }

  const alarm = await alarmGet(REFRESH_ALARM);
  if (!alarm) {
    refreshState.nextRefreshAt = Date.now() + refreshState.intervalSec * 1000;
    await scheduleRefreshAlarm();
    await saveRefreshState();
  }
}

async function saveRefreshState() {
  await chrome.storage.local.set({ sacRefresh: refreshState });
}

async function clearRefreshAlarm() {
  await alarmClear(REFRESH_ALARM);
}

async function scheduleRefreshAlarm() {
  await clearRefreshAlarm();
  if (!refreshState.running || !refreshState.tabId) return;

  const delayMin = Math.max(0.0167, refreshState.intervalSec / 60);
  await alarmCreate(REFRESH_ALARM, { delayInMinutes: delayMin });
  refreshState.nextRefreshAt = Date.now() + refreshState.intervalSec * 1000;
}

async function performRefreshTick() {
  if (!refreshState.running || !refreshState.tabId) return;

  try {
    const tab = await chrome.tabs.get(refreshState.tabId);
    if (!canReloadTab(tab)) {
      await stopAutoRefresh();
      return;
    }
    await chrome.tabs.reload(refreshState.tabId);
    refreshState.refreshCount += 1;
    refreshState.nextRefreshAt = Date.now() + refreshState.intervalSec * 1000;
    await saveRefreshState();
    broadcastRefreshState();
    await scheduleRefreshAlarm();
  } catch {
    await stopAutoRefresh();
  }
}

async function stopAutoRefresh() {
  refreshState.running = false;
  refreshState.nextRefreshAt = null;
  await clearRefreshAlarm();
  await saveRefreshState();
  broadcastRefreshState();
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

async function startAutoRefresh(intervalSec, tabId) {
  const tab = await resolveTargetTab(tabId);
  if (!tab?.id || !canReloadTab(tab)) {
    return { ok: false, error: "invalid_tab", url: tab?.url || "" };
  }

  refreshState.intervalSec = clampRefreshSec(intervalSec);
  refreshState.tabId = tab.id;
  refreshState.running = true;

  try {
    await chrome.tabs.reload(refreshState.tabId);
    refreshState.refreshCount = (refreshState.refreshCount || 0) + 1;
    await scheduleRefreshAlarm();
    await saveRefreshState();
    broadcastRefreshState();
    return { ok: true, tabId: tab.id };
  } catch (err) {
    refreshState.running = false;
    refreshState.nextRefreshAt = null;
    refreshState.tabId = null;
    await clearRefreshAlarm();
    await saveRefreshState();
    broadcastRefreshState();
    return { ok: false, error: "start_failed", message: String(err?.message || err) };
  }
}

async function initRefreshFromStorage() {
  await sanitizeRefreshState();
  if (refreshState.running && refreshState.tabId) {
    try {
      if (!refreshState.nextRefreshAt || refreshState.nextRefreshAt < Date.now()) {
        refreshState.nextRefreshAt = Date.now() + refreshState.intervalSec * 1000;
      }
      await scheduleRefreshAlarm();
      broadcastRefreshState();
    } catch {
      await stopAutoRefresh();
    }
  }
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === REFRESH_ALARM) {
    loadRefreshState().then(() => performRefreshTick());
    return;
  }
  if (alarm.name === CLEAR_ALARM) {
    loadClearState().then(() => performClearTick());
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (refreshState.tabId === tabId) stopAutoRefresh();
  if (clearState.tabId === tabId) stopAutoClear();
});

// --- Auto Clear Site Data ---
const CLEAR_ALARM = "sac-auto-clear";

const DEFAULT_CLEAR_STATE = {
  running: false,
  intervalSec: 10,
  tabId: null,
  origin: null,
  clearCount: 0,
  nextRunAt: null,
};

let clearState = { ...DEFAULT_CLEAR_STATE };

const BROWSING_DATA_REMOVE_OPTS = {
  cache: true,
  cacheStorage: true,
  cookies: true,
  fileSystems: true,
  indexedDB: true,
  localStorage: true,
  serviceWorkers: true,
  webSQL: true,
};

function getOriginFromUrl(url) {
  return new URL(url).origin;
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

async function loadClearState() {
  const data = await chrome.storage.local.get("sacClear");
  if (data.sacClear) {
    clearState = { ...DEFAULT_CLEAR_STATE, ...data.sacClear };
  }
}

async function saveClearState() {
  await chrome.storage.local.set({ sacClear: clearState });
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
    secondsUntilNext,
  };
}

function broadcastClearState() {
  chrome.runtime.sendMessage({
    type: "CLEAR_STATE_UPDATE",
    state: getPublicClearState(),
  }).catch(() => {});
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
    await stopAutoClear();
    return;
  }

  try {
    const tab = await chrome.tabs.get(clearState.tabId);
    if (!canReloadTab(tab)) {
      await stopAutoClear();
      return;
    }
    clearState.origin = getOriginFromUrl(tab.url);
  } catch {
    await stopAutoClear();
    return;
  }

  const alarm = await alarmGet(CLEAR_ALARM);
  if (!alarm) {
    clearState.nextRunAt = Date.now() + clearState.intervalSec * 1000;
    await scheduleClearAlarm();
    await saveClearState();
  }
}

async function clearSiteDataAndReload(tabId) {
  const tab = await chrome.tabs.get(tabId);
  if (!canReloadTab(tab)) throw new Error("invalid_tab");

  const origin = getOriginFromUrl(tab.url);
  await browsingDataRemove([origin], BROWSING_DATA_REMOVE_OPTS);
  await chrome.tabs.reload(tabId);
  return origin;
}

async function performClearTick() {
  if (!clearState.running || !clearState.tabId) return;

  try {
    clearState.origin = await clearSiteDataAndReload(clearState.tabId);
    clearState.clearCount += 1;
    await scheduleClearAlarm();
    await saveClearState();
    broadcastClearState();
  } catch {
    await stopAutoClear();
  }
}

async function stopAutoClear() {
  clearState.running = false;
  clearState.nextRunAt = null;
  await alarmClear(CLEAR_ALARM);
  await saveClearState();
  broadcastClearState();
}

async function startAutoClear(intervalSec, tabId) {
  const tab = await resolveTargetTab(tabId);
  if (!tab?.id || !canReloadTab(tab)) {
    return { ok: false, error: "invalid_tab", url: tab?.url || "" };
  }

  clearState.intervalSec = clampRefreshSec(intervalSec);
  clearState.tabId = tab.id;
  clearState.origin = getOriginFromUrl(tab.url);
  clearState.running = true;

  try {
    await clearSiteDataAndReload(clearState.tabId);
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
    return { ok: false, error: "start_failed", message: String(err?.message || err) };
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

Promise.all([
  loadState().then(broadcastState),
  initRefreshFromStorage(),
  initClearFromStorage(),
]);
