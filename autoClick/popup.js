const TARGET_SVG = `<svg class="target-icon" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
  <circle cx="12" cy="12" r="1.5" fill="#A855F7"/>
  <circle cx="12" cy="12" r="5" stroke="#A855F7" stroke-width="2"/>
  <line x1="12" y1="2" x2="12" y2="5" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
  <line x1="12" y1="19" x2="12" y2="22" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
  <line x1="2" y1="12" x2="5" y2="12" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
  <line x1="19" y1="12" x2="22" y2="12" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
</svg>`;

const $ = (id) => document.getElementById(id);

let localState = null;
let intervalStep = 0.1;

function send(type, payload = {}, timeoutMs = 10000) {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      console.warn("sendMessage timeout:", type);
      resolve(undefined);
    }, timeoutMs);

    chrome.runtime.sendMessage({ type, ...payload }, (response) => {
      clearTimeout(timer);
      if (chrome.runtime.lastError) {
        console.warn("sendMessage:", chrome.runtime.lastError.message);
        resolve(undefined);
        return;
      }
      resolve(response);
    });
  });
}

function resetRefreshStartButton(running = false) {
  const startBtn = $("btnRefreshStart");
  startBtn.dataset.busy = "0";
  startBtn.textContent = running ? "↻ CHẠY LẠI" : "▶ BẮT ĐẦU";
}

async function getTargetTab() {
  let tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tabs[0]?.id) return tabs[0];
  tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

function parseDelay(val, fallback = 1) {
  const n = parseFloat(String(val).trim().replace(",", "."));
  if (!Number.isFinite(n)) return fallback;
  return Math.round(Math.max(0.1, Math.min(300, n)) * 10) / 10;
}

function isEditingDelay() {
  const el = document.activeElement;
  return (
    el?.id === "intervalInput" ||
    el?.classList?.contains("pt-delay-input") ||
    el?.classList?.contains("txt-delay-input")
  );
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderPointsList(s) {
  const list = $("pointsList");
  const empty = $("pointsEmpty");
  list.querySelectorAll(".point-item").forEach((el) => el.remove());

  if (!s.points?.length) {
    empty.style.display = "block";
    return;
  }

  empty.style.display = "none";
  s.points.forEach((pt, i) => {
    const delay = (pt.delay ?? s.interval ?? 1).toFixed(1);
    const li = document.createElement("li");
    li.className = "point-item";
    li.innerHTML = `
      ${TARGET_SVG}
      <span class="point-coords">${i + 1}: (${pt.x}, ${pt.y})</span>
      <div class="point-delay">
        <input
          type="number"
          class="delay-input pt-delay-input"
          data-index="${i}"
          min="0.1"
          max="300"
          step="0.1"
          value="${delay}"
          aria-label="Delay point ${i + 1} (giây)"
        />
        <span class="pt-delay-unit">s</span>
      </div>
      <button type="button" class="point-delete" data-index="${i}" aria-label="Remove point">✕</button>
    `;
    list.appendChild(li);
  });
}

function renderTextList(s) {
  const list = $("textList");
  const empty = $("textEmpty");
  list.querySelectorAll(".text-target-item").forEach((el) => el.remove());

  if (!s.textTargets?.length) {
    empty.style.display = "block";
    return;
  }

  empty.style.display = "none";
  s.textTargets.forEach((t, i) => {
    const delay = (t.delay ?? s.interval ?? 1).toFixed(1);
    const label = t.text.length > 42 ? `${t.text.slice(0, 42)}…` : t.text;
    const li = document.createElement("li");
    li.className = "point-item text-target-item";
    li.innerHTML = `
      <span class="text-icon">T</span>
      <span class="text-label" title="${escapeHtml(t.text)}">${i + 1}: "${escapeHtml(label)}"</span>
      <div class="point-delay">
        <input
          type="number"
          class="delay-input txt-delay-input"
          data-index="${i}"
          min="0.1"
          max="300"
          step="0.1"
          value="${delay}"
          aria-label="Delay text ${i + 1}"
        />
        <span class="pt-delay-unit">s</span>
      </div>
      <button type="button" class="point-delete text-delete" data-index="${i}" aria-label="Remove text">✕</button>
    `;
    list.appendChild(li);
  });
}

function renderClickMode(mode) {
  document.querySelectorAll(".mode-chip").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === mode);
  });
}

function render(s) {
  localState = s;
  intervalStep = s.intervalStep ?? 0.1;

  const seqCount = s.sequenceCount ?? s.points?.length ?? 0;
  $("statPoints").textContent = String(seqCount);
  $("statCycles").textContent = String(s.stats?.cycles ?? 0);
  $("statClicks").textContent = String(s.stats?.totalClicks ?? 0);
  $("statTime").textContent = s.elapsedFormatted ?? "00:00";

  const intervalInput = $("intervalInput");
  if (document.activeElement !== intervalInput) {
    intervalInput.value = (s.interval ?? 1).toFixed(1);
  }

  $("humanizeToggle").checked = s.humanize !== false;
  renderClickMode(s.clickMode || "coords");

  const running = s.running;
  const capturing = s.capturing;
  const capturingText = s.capturingText;

  $("statusDot").className =
    "status-dot" +
    (capturing || capturingText ? " capturing" : running ? " running" : "");
  $("statusText").textContent = capturingText
    ? "Thêm chữ"
    : capturing
      ? "Thêm điểm"
      : running
        ? "Running"
        : "Paused";

  const toggleBtn = $("btnToggle");
  const toggleIcon = $("toggleIcon");
  const toggleLabel = $("toggleLabel");
  if (running) {
    toggleBtn.classList.add("paused-mode");
    toggleIcon.textContent = "⏸";
    toggleLabel.textContent = "PAUSE";
  } else {
    toggleBtn.classList.remove("paused-mode");
    toggleIcon.textContent = "▶";
    toggleLabel.textContent = "RESUME";
  }

  $("btnAddPoints").classList.toggle("capturing", capturing);
  $("btnAddText").classList.toggle("capturing", capturingText);
  $("btnWidget").classList.toggle("active", s.widgetVisible);

  if (!isEditingDelay()) {
    renderPointsList(s);
    renderTextList(s);
  }

  $("maxCycles").value = s.maxCycles ?? 0;
  $("intervalStep").value = intervalStep;
  $("showMarkers").checked = s.showMarkers !== false;
  $("clickSequence").checked = s.clickSequence !== false;
}

async function refresh() {
  const s = await send("GET_STATE");
  if (s) render(s);
}

async function commitDefaultInterval() {
  const input = $("intervalInput");
  const val = parseDelay(input.value, localState?.interval ?? 1);
  input.value = val.toFixed(1);
  if (localState && Math.abs(val - (localState.interval ?? 1)) < 0.001) return;
  await send("SET_INTERVAL", { interval: val });
  await refresh();
}

async function commitPointDelay(index, rawValue) {
  const pt = localState?.points?.[index];
  if (!pt) return;

  const val = parseDelay(rawValue, pt.delay ?? localState?.interval ?? 1);
  const input = $("pointsList").querySelector(`.pt-delay-input[data-index="${index}"]`);
  if (input) input.value = val.toFixed(1);

  const current = pt.delay ?? localState?.interval ?? 1;
  if (Math.abs(val - current) < 0.001) return;

  await send("SET_POINT_DELAY", { index, delay: val });
  await refresh();
}

function onDelayInputKeydown(e, commitFn) {
  if (e.key === "Enter") {
    e.preventDefault();
    e.target.blur();
    commitFn();
  }
}

$("intervalInput").addEventListener("change", commitDefaultInterval);
$("intervalInput").addEventListener("blur", commitDefaultInterval);
$("intervalInput").addEventListener("keydown", (e) => {
  onDelayInputKeydown(e, commitDefaultInterval);
});

$("humanizeToggle").addEventListener("change", async (e) => {
  await send("SET_HUMANIZE", { enabled: e.target.checked });
  refresh();
});

$("btnAddPoints").addEventListener("click", async () => {
  const next = !localState?.capturing;
  if (next) await send("SET_TEXT_CAPTURE", { enabled: false });
  await send("SET_CAPTURE", { enabled: next });
  window.close();
});

$("btnAddText").addEventListener("click", async () => {
  const next = !localState?.capturingText;
  if (next) await send("SET_CAPTURE", { enabled: false });
  await send("SET_TEXT_CAPTURE", { enabled: next });
  window.close();
});

document.querySelectorAll(".mode-chip").forEach((btn) => {
  btn.addEventListener("click", async () => {
    await send("SET_CLICK_MODE", { mode: btn.dataset.mode });
    await refresh();
  });
});

$("btnToggle").addEventListener("click", async () => {
  await send("TOGGLE_RUNNING");
  refresh();
});

$("btnReset").addEventListener("click", async () => {
  await send("RESET_STATS");
  refresh();
});

$("btnWidget").addEventListener("click", async () => {
  await send("TOGGLE_WIDGET");
  refresh();
  window.close();
});

$("btnClearPoints").addEventListener("click", async () => {
  if (confirm("Clear all points?")) {
    await send("CLEAR_POINTS");
    refresh();
  }
});

async function commitTextDelay(index, rawValue) {
  const t = localState?.textTargets?.[index];
  if (!t) return;

  const val = parseDelay(rawValue, t.delay ?? localState?.interval ?? 1);
  const input = $("textList").querySelector(`.txt-delay-input[data-index="${index}"]`);
  if (input) input.value = val.toFixed(1);

  const current = t.delay ?? localState?.interval ?? 1;
  if (Math.abs(val - current) < 0.001) return;

  await send("SET_TEXT_TARGET_DELAY", { index, delay: val });
  await refresh();
}

$("btnClearText").addEventListener("click", async () => {
  if (confirm("Xóa toàn bộ danh sách chữ?")) {
    await send("CLEAR_TEXT_TARGETS");
    refresh();
  }
});

$("btnApplyTextDefault").addEventListener("click", async () => {
  if (!localState?.textTargets?.length) return;
  if (confirm("Gán default delay cho TẤT CẢ chữ?")) {
    await send("APPLY_DEFAULT_TEXT_DELAY");
    refresh();
  }
});

$("textList").addEventListener("click", async (e) => {
  const delBtn = e.target.closest(".text-delete");
  if (!delBtn) return;
  await send("REMOVE_TEXT_TARGET", { index: parseInt(delBtn.dataset.index, 10) });
  refresh();
});

$("textList").addEventListener("change", async (e) => {
  const input = e.target.closest(".txt-delay-input");
  if (!input) return;
  await commitTextDelay(parseInt(input.dataset.index, 10), input.value);
});

$("textList").addEventListener("blur", async (e) => {
  const input = e.target.closest(".txt-delay-input");
  if (!input) return;
  await commitTextDelay(parseInt(input.dataset.index, 10), input.value);
}, true);

$("textList").addEventListener("keydown", (e) => {
  const input = e.target.closest(".txt-delay-input");
  if (!input) return;
  onDelayInputKeydown(e, () => commitTextDelay(parseInt(input.dataset.index, 10), input.value));
});

$("pointsList").addEventListener("click", async (e) => {
  const delBtn = e.target.closest(".point-delete");
  if (!delBtn || delBtn.classList.contains("text-delete")) return;
  const index = parseInt(delBtn.dataset.index, 10);
  await send("REMOVE_POINT", { index });
  refresh();
});

$("pointsList").addEventListener("change", async (e) => {
  const input = e.target.closest(".pt-delay-input");
  if (!input) return;
  await commitPointDelay(parseInt(input.dataset.index, 10), input.value);
});

$("pointsList").addEventListener("blur", async (e) => {
  const input = e.target.closest(".pt-delay-input");
  if (!input) return;
  await commitPointDelay(parseInt(input.dataset.index, 10), input.value);
}, true);

$("pointsList").addEventListener("keydown", (e) => {
  const input = e.target.closest(".pt-delay-input");
  if (!input) return;
  onDelayInputKeydown(e, () => commitPointDelay(parseInt(input.dataset.index, 10), input.value));
});

$("btnApplyDefault").addEventListener("click", async () => {
  if (!localState?.points?.length) return;
  if (confirm("Gán default delay cho TẤT CẢ point?")) {
    await send("APPLY_DEFAULT_DELAY");
    refresh();
  }
});

$("btnSettings").addEventListener("click", () => {
  $("settingsModal").showModal();
});

$("settingsCancel").addEventListener("click", () => {
  $("settingsModal").close();
});

$("settingsModal").addEventListener("close", async () => {
  if ($("settingsModal").returnValue !== "default") return;
  await send("UPDATE_SETTINGS", {
    maxCycles: parseInt($("maxCycles").value, 10) || 0,
    intervalStep: parseFloat($("intervalStep").value) || 0.1,
    showMarkers: $("showMarkers").checked,
    clickSequence: $("clickSequence").checked,
  });
  refresh();
});

$("linkShortcuts").addEventListener("click", () => {
  $("shortcutsModal").showModal();
});

$("linkSupport").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://github.com" });
});

$("linkDonate").addEventListener("click", () => {
  chrome.tabs.create({ url: "https://www.buymeacoffee.com" });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "STATE_UPDATE" && !isEditingDelay()) render(msg.state);
  if (msg.type === "REFRESH_STATE_UPDATE") renderRefresh(msg.state);
  if (msg.type === "CLEAR_STATE_UPDATE") renderClear(msg.state);
  if (msg.type === "SAVED") {
    const badge = $("savedBadge");
    badge.classList.add("visible");
    setTimeout(() => badge.classList.remove("visible"), 1500);
  }
});

// --- Tabs ---
let refreshLocal = null;
let refreshCountdownTimer = null;
let selectedRefreshSec = 10;

function formatRefreshLabel(sec) {
  if (sec >= 60 && sec % 60 === 0) return `${sec / 60}p`;
  return `${sec}s`;
}

function switchTab(name) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("tabClicker").classList.toggle("active", name === "clicker");
  $("tabClicker").hidden = name !== "clicker";
  $("tabRefresh").classList.toggle("active", name === "refresh");
  $("tabRefresh").hidden = name !== "refresh";
  $("tabClear").classList.toggle("active", name === "clear");
  $("tabClear").hidden = name !== "clear";

  if (name === "refresh") {
    refreshRefreshState();
    startRefreshCountdownTicker();
    stopClearCountdownTicker();
  } else if (name === "clear") {
    refreshClearState();
    startClearCountdownTicker();
    stopRefreshCountdownTicker();
  } else {
    stopRefreshCountdownTicker();
    stopClearCountdownTicker();
  }

  try {
    chrome.storage.local.set({ sacActiveTab: name });
  } catch {
    /* ignore */
  }
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

chrome.storage.local.get("sacActiveTab").then((data) => {
  if (data.sacActiveTab === "refresh" || data.sacActiveTab === "clear") {
    switchTab(data.sacActiveTab);
  }
});

// --- Auto Refresh ---
function highlightRefreshPreset(sec) {
  selectedRefreshSec = sec;
  document.querySelectorAll(".refresh-preset-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.sec, 10) === sec);
  });
  const input = $("refreshCustomInput");
  if (document.activeElement !== input) input.value = String(sec);
}

function renderRefresh(rs) {
  if (!rs) return;
  refreshLocal = rs;
  selectedRefreshSec = rs.intervalSec ?? selectedRefreshSec;

  $("refreshCount").textContent = String(rs.refreshCount ?? 0);
  $("refreshIntervalLabel").textContent = formatRefreshLabel(rs.intervalSec ?? 10);

  const running = !!rs.running;
  $("refreshStatusDot").className = "status-dot" + (running ? " running" : "");
  $("refreshStatusText").textContent = running ? "Running" : "Stopped";

  const startBtn = $("btnRefreshStart");
  const stopBtn = $("btnRefreshStop");
  startBtn.disabled = false;
  startBtn.textContent = running ? "↻ CHẠY LẠI" : "▶ BẮT ĐẦU";
  stopBtn.disabled = !running;

  highlightRefreshPreset(rs.intervalSec ?? selectedRefreshSec);
  updateRefreshCountdownDisplay(rs);
}

function updateRefreshCountdownDisplay(rs) {
  const el = $("refreshCountdown");
  if (!rs?.running) {
    el.textContent = "—";
    return;
  }
  const s = rs.secondsUntilNext;
  el.textContent = s != null ? `${s}s` : "…";
}

function startRefreshCountdownTicker() {
  stopRefreshCountdownTicker();
  refreshCountdownTimer = setInterval(async () => {
    if ($("tabRefresh").hidden) return;
    const rs = await send("GET_REFRESH_STATE");
    if (rs) updateRefreshCountdownDisplay(rs);
  }, 1000);
}

function stopRefreshCountdownTicker() {
  if (refreshCountdownTimer) {
    clearInterval(refreshCountdownTimer);
    refreshCountdownTimer = null;
  }
}

async function refreshRefreshState() {
  const rs = await send("GET_REFRESH_STATE");
  if (rs) renderRefresh(rs);

  const tab = await getTargetTab();
  const hint = $("refreshHint");
  try {
    if (tab?.url && (tab.url.startsWith("http://") || tab.url.startsWith("https://"))) {
      hint.textContent = `Tab: ${new URL(tab.url).hostname} — sẵn sàng refresh`;
      hint.style.color = "#2dd4bf";
    } else {
      hint.textContent = "Mở tab http/https (không phải chrome://) rồi bấm BẮT ĐẦU.";
      hint.style.color = "#f87171";
    }
  } catch {
    hint.textContent = "Mở tab http/https rồi bấm BẮT ĐẦU.";
    hint.style.color = "#f87171";
  }
}

function parseRefreshSec(val) {
  const n = parseInt(String(val).trim(), 10);
  if (!Number.isFinite(n)) return selectedRefreshSec;
  return Math.max(1, Math.min(3600, n));
}

document.querySelectorAll(".refresh-preset-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const sec = parseInt(btn.dataset.sec, 10);
    highlightRefreshPreset(sec);
    await send("SET_REFRESH_INTERVAL", { intervalSec: sec });
    await refreshRefreshState();
  });
});

$("refreshCustomInput").addEventListener("change", async () => {
  const sec = parseRefreshSec($("refreshCustomInput").value);
  highlightRefreshPreset(sec);
  await send("SET_REFRESH_INTERVAL", { intervalSec: sec });
  await refreshRefreshState();
});

$("refreshCustomInput").addEventListener("blur", async () => {
  const sec = parseRefreshSec($("refreshCustomInput").value);
  highlightRefreshPreset(sec);
  await send("SET_REFRESH_INTERVAL", { intervalSec: sec });
  await refreshRefreshState();
});

$("refreshCustomInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    e.target.blur();
  }
});

$("btnRefreshStart").addEventListener("click", async () => {
  const startBtn = $("btnRefreshStart");
  if (startBtn.dataset.busy === "1") return;

  const sec = parseRefreshSec($("refreshCustomInput").value);
  const tab = await getTargetTab();

  startBtn.dataset.busy = "1";
  startBtn.textContent = "Đang bật…";

  let started = false;
  try {
    const res = await send("START_AUTO_REFRESH", { intervalSec: sec, tabId: tab?.id }, 12000);

    if (!res) {
      alert(
        "Extension không phản hồi.\nVào chrome://extensions/ → Reload Speed Auto Clicker."
      );
      return;
    }

    if (!res.ok) {
      const urlHint = tab?.url ? `\n\nTab hiện tại: ${tab.url}` : "";
      const msg = res.message ? `\n\nChi tiết: ${res.message}` : "";
      alert(
        "Không bắt đầu được auto refresh.\nMở tab http/https (không phải chrome://) rồi thử lại." +
          urlHint +
          msg
      );
      return;
    }

    started = true;
    await refreshRefreshState();
    startRefreshCountdownTicker();
  } finally {
    if (!started) resetRefreshStartButton(false);
    else {
      const rs = await send("GET_REFRESH_STATE", {}, 5000);
      if (rs) renderRefresh(rs);
      else resetRefreshStartButton(true);
    }
  }
});

$("btnRefreshStop").addEventListener("click", async () => {
  await send("STOP_AUTO_REFRESH");
  await refreshRefreshState();
});

$("btnRefreshReset").addEventListener("click", async () => {
  await send("RESET_REFRESH_COUNT");
  await refreshRefreshState();
});

// --- Auto Clear Site Data ---
let selectedClearSec = 10;
let clearCountdownTimer = null;

function formatIntervalLabel(sec) {
  if (sec >= 60 && sec % 60 === 0) return `${sec / 60}p`;
  return `${sec}s`;
}

function highlightClearPreset(sec) {
  selectedClearSec = sec;
  document.querySelectorAll(".clear-preset-btn").forEach((btn) => {
    btn.classList.toggle("active", parseInt(btn.dataset.sec, 10) === sec);
  });
  const input = $("clearCustomInput");
  if (document.activeElement !== input) input.value = String(sec);
}

function resetClearStartButton(running = false) {
  const btn = $("btnClearStart");
  btn.dataset.busy = "0";
  btn.textContent = running ? "↻ CHẠY LẠI" : "▶ BẮT ĐẦU";
}

function renderClear(cs) {
  if (!cs) return;
  selectedClearSec = cs.intervalSec ?? selectedClearSec;

  $("clearCount").textContent = String(cs.clearCount ?? 0);
  $("clearIntervalLabel").textContent = formatIntervalLabel(cs.intervalSec ?? 10);

  const running = !!cs.running;
  $("clearStatusDot").className = "status-dot" + (running ? " running" : "");
  $("clearStatusText").textContent = running ? "Running" : "Stopped";

  $("btnClearStart").disabled = false;
  $("btnClearStart").textContent = running ? "↻ CHẠY LẠI" : "▶ BẮT ĐẦU";
  $("btnClearStop").disabled = !running;

  highlightClearPreset(cs.intervalSec ?? selectedClearSec);
  updateClearCountdownDisplay(cs);
}

function updateClearCountdownDisplay(cs) {
  const el = $("clearCountdown");
  if (!cs?.running) {
    el.textContent = "—";
    return;
  }
  const s = cs.secondsUntilNext;
  el.textContent = s != null ? `${s}s` : "…";
}

function startClearCountdownTicker() {
  stopClearCountdownTicker();
  clearCountdownTimer = setInterval(async () => {
    if ($("tabClear").hidden) return;
    const cs = await send("GET_CLEAR_STATE");
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
  const cs = await send("GET_CLEAR_STATE");
  if (cs) renderClear(cs);

  const tab = await getTargetTab();
  const hint = $("clearHint");
  try {
    if (tab?.url && (tab.url.startsWith("http://") || tab.url.startsWith("https://"))) {
      const host = new URL(tab.url).hostname;
      hint.textContent = `Sẽ xóa dữ liệu: ${host} (cookies, cache, storage…)`;
      hint.style.color = "#2dd4bf";
    } else {
      hint.textContent = "Mở tab http/https rồi bấm BẮT ĐẦU.";
      hint.style.color = "#f87171";
    }
  } catch {
    hint.textContent = "Mở tab http/https rồi bấm BẮT ĐẦU.";
    hint.style.color = "#f87171";
  }
}

function parseClearSec(val) {
  const n = parseInt(String(val).trim(), 10);
  if (!Number.isFinite(n)) return selectedClearSec;
  return Math.max(1, Math.min(3600, n));
}

document.querySelectorAll(".clear-preset-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const sec = parseInt(btn.dataset.sec, 10);
    highlightClearPreset(sec);
    await send("SET_CLEAR_INTERVAL", { intervalSec: sec });
    await refreshClearState();
  });
});

$("clearCustomInput").addEventListener("change", async () => {
  const sec = parseClearSec($("clearCustomInput").value);
  highlightClearPreset(sec);
  await send("SET_CLEAR_INTERVAL", { intervalSec: sec });
  await refreshClearState();
});

$("clearCustomInput").addEventListener("blur", async () => {
  const sec = parseClearSec($("clearCustomInput").value);
  highlightClearPreset(sec);
  await send("SET_CLEAR_INTERVAL", { intervalSec: sec });
  await refreshClearState();
});

$("clearCustomInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    e.target.blur();
  }
});

$("btnClearStart").addEventListener("click", async () => {
  const startBtn = $("btnClearStart");
  if (startBtn.dataset.busy === "1") return;

  const sec = parseClearSec($("clearCustomInput").value);
  const tab = await getTargetTab();

  startBtn.dataset.busy = "1";
  startBtn.textContent = "Đang bật…";

  let started = false;
  try {
    const res = await send("START_AUTO_CLEAR", { intervalSec: sec, tabId: tab?.id }, 15000);

    if (!res) {
      alert("Extension không phản hồi.\nReload extension tại chrome://extensions/");
      return;
    }

    if (!res.ok) {
      const urlHint = tab?.url ? `\n\nTab: ${tab.url}` : "";
      const msg = res.message ? `\n\n${res.message}` : "";
      alert("Không bắt đầu được.\nMở tab http/https và cấp quyền browsingData." + urlHint + msg);
      return;
    }

    started = true;
    await refreshClearState();
    startClearCountdownTicker();
  } finally {
    if (!started) resetClearStartButton(false);
    else {
      const cs = await send("GET_CLEAR_STATE", {}, 5000);
      if (cs) renderClear(cs);
      else resetClearStartButton(true);
    }
  }
});

$("btnClearStop").addEventListener("click", async () => {
  await send("STOP_AUTO_CLEAR");
  await refreshClearState();
});

$("btnClearReset").addEventListener("click", async () => {
  await send("RESET_CLEAR_COUNT");
  await refreshClearState();
});

resetRefreshStartButton(false);
resetClearStartButton(false);
refresh();
refreshRefreshState();
