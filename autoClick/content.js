(function () {
  const ROOT_ID = "sac-overlay-root";
  const WIDGET_ID = "sac-floating-widget";

  const CROSSHAIR_SVG = `<svg class="sac-crosshair-svg" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
    <circle cx="12" cy="12" r="1.5" fill="#A855F7"/>
    <circle cx="12" cy="12" r="5" stroke="#A855F7" stroke-width="2"/>
    <line x1="12" y1="2" x2="12" y2="5" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
    <line x1="12" y1="19" x2="12" y2="22" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
    <line x1="2" y1="12" x2="5" y2="12" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
    <line x1="19" y1="12" x2="22" y2="12" stroke="#A855F7" stroke-width="2" stroke-linecap="round"/>
  </svg>`;

  let state = {
    capturing: false,
    capturingText: false,
    points: [],
    textTargets: [],
    showMarkers: true,
    widgetVisible: false,
  };

  function getRoot() {
    let root = document.getElementById(ROOT_ID);
    if (!root) {
      root = document.createElement("div");
      root.id = ROOT_ID;
      document.documentElement.appendChild(root);
    }
    return root;
  }

  function elementText(el) {
    return (el.innerText || el.textContent || "").trim().replace(/\s+/g, " ");
  }

  function isElementVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || Number(st.opacity) === 0) return false;
    return true;
  }

  function renderMarkers() {
    const root = getRoot();
    root.querySelectorAll(".sac-marker").forEach((el) => el.remove());
    if (!state.showMarkers || !state.points.length) return;

    state.points.forEach((pt) => {
      const marker = document.createElement("div");
      marker.className = "sac-marker";
      marker.style.left = `${pt.x}px`;
      marker.style.top = `${pt.y}px`;
      marker.innerHTML = CROSSHAIR_SVG;
      root.appendChild(marker);
    });
  }

  function findElementForText(searchText, exact) {
    const candidates = [];
    const nodes = document.querySelectorAll(
      "button, a, [role='button'], input[type='button'], input[type='submit'], label, span, div, p, li, td, th, h1, h2, h3, h4, h5, h6, em, strong"
    );

    nodes.forEach((el) => {
      if (!isElementVisible(el)) return;
      const t = elementText(el);
      if (!t) return;
      const match = exact ? t === searchText : t.includes(searchText);
      if (!match) return;
      const r = el.getBoundingClientRect();
      candidates.push({ el, area: r.width * r.height });
    });

    if (!candidates.length) return null;
    candidates.sort((a, b) => a.area - b.area);
    return candidates[0].el;
  }

  function renderTextHighlights() {
    const root = getRoot();
    root.querySelectorAll(".sac-text-highlight").forEach((el) => el.remove());
    if (!state.showMarkers || !state.textTargets?.length) return;

    state.textTargets.forEach((target, i) => {
      const el = findElementForText(target.text, target.exact !== false);
      if (!el) return;
      const r = el.getBoundingClientRect();
      const box = document.createElement("div");
      box.className = "sac-text-highlight";
      box.title = target.text;
      box.style.left = `${r.left}px`;
      box.style.top = `${r.top}px`;
      box.style.width = `${r.width}px`;
      box.style.height = `${r.height}px`;
      box.innerHTML = `<span class="sac-text-badge">${i + 1}</span>`;
      root.appendChild(box);
    });
  }

  function renderCaptureOverlay() {
    const root = getRoot();
    let overlay = root.querySelector(".sac-capture-overlay");
    const active = state.capturing || state.capturingText;

    if (!active) {
      overlay?.remove();
      document.body.style.cursor = "";
      return;
    }

    if (!overlay) {
      overlay = document.createElement("div");
      overlay.className = "sac-capture-overlay";
      root.appendChild(overlay);
    }

    if (state.capturingText) {
      overlay.innerHTML = `<div class="sac-capture-hint sac-capture-text">Click vào chữ trên trang để thêm · <kbd>Esc</kbd> xong</div>`;
      document.body.style.cursor = "text";
    } else {
      overlay.innerHTML = `<div class="sac-capture-hint">Click để thêm điểm · <kbd>Esc</kbd> xong</div>`;
      document.body.style.cursor = "crosshair";
    }
  }

  function performClick(x, y) {
    const el = document.elementFromPoint(x, y);
    if (!el) return false;

    const opts = {
      bubbles: true,
      cancelable: true,
      view: window,
      clientX: x,
      clientY: y,
      screenX: window.screenX + x,
      screenY: window.screenY + y,
      button: 0,
      buttons: 1,
    };

    ["pointerdown", "mousedown", "pointerup", "mouseup", "click"].forEach((type) => {
      const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      el.dispatchEvent(new Ctor(type, { ...opts, detail: type === "click" ? 1 : 0 }));
    });
    return true;
  }

  function clickElementCenter(el) {
    const r = el.getBoundingClientRect();
    return performClick(r.left + r.width / 2, r.top + r.height / 2);
  }

  function getTextFromClickTarget(start) {
    let el = start;
    for (let i = 0; i < 10 && el; i++) {
      const text = elementText(el);
      if (text.length >= 1 && text.length <= 200) return text;
      el = el.parentElement;
    }
    return "";
  }

  function onCaptureClick(e) {
    if (!state.capturing && !state.capturingText) return;
    if (e.target.closest(`#${ROOT_ID}`) || e.target.closest(`#${WIDGET_ID}`)) return;

    e.preventDefault();
    e.stopPropagation();

    if (state.capturingText) {
      const text = getTextFromClickTarget(e.target);
      if (!text) return;
      if (!state.textTargets.some((t) => t.text === text)) {
        state.textTargets.push({ text, exact: true });
        renderTextHighlights();
      }
      chrome.runtime.sendMessage({ type: "TEXT_TARGET_ADDED", target: { text, exact: true } });
      return;
    }

    if (state.capturing) {
      const point = { x: Math.round(e.clientX), y: Math.round(e.clientY) };
      state.points.push(point);
      chrome.runtime.sendMessage({ type: "POINT_ADDED", point });
      renderMarkers();
    }
  }

  function onCaptureKey(e) {
    if (e.key === "Escape" && (state.capturing || state.capturingText)) {
      state.capturing = false;
      state.capturingText = false;
      renderCaptureOverlay();
      chrome.runtime.sendMessage({ type: "CAPTURE_STOPPED" });
    }
  }

  document.addEventListener("click", onCaptureClick, true);
  document.addEventListener("keydown", onCaptureKey, true);

  const WIDGET_POS_KEY = "sacWidgetPos";

  function clampWidgetPos(left, top, width, height) {
    const maxLeft = Math.max(0, window.innerWidth - width);
    const maxTop = Math.max(0, window.innerHeight - height);
    return {
      left: Math.min(maxLeft, Math.max(0, left)),
      top: Math.min(maxTop, Math.max(0, top)),
    };
  }

  function applyWidgetPosition(widget, left, top) {
    const rect = widget.getBoundingClientRect();
    const pos = clampWidgetPos(left, top, rect.width, rect.height);
    widget.style.right = "auto";
    widget.style.bottom = "auto";
    widget.style.left = `${pos.left}px`;
    widget.style.top = `${pos.top}px`;
    return pos;
  }

  function saveWidgetPosition(left, top) {
    chrome.storage.local.set({ [WIDGET_POS_KEY]: { left, top } }).catch(() => {});
  }

  function loadWidgetPosition(callback) {
    chrome.storage.local.get(WIDGET_POS_KEY).then((data) => {
      callback(data[WIDGET_POS_KEY] || null);
    }).catch(() => callback(null));
  }

  function bindWidgetDrag(widget) {
    if (widget.dataset.dragBound) return;
    widget.dataset.dragBound = "1";

    const header = widget.querySelector(".sac-widget-header");
    let dragging = false;
    let pointerId = null;
    let offsetX = 0;
    let offsetY = 0;

    const onPointerMove = (ev) => {
      if (!dragging || ev.pointerId !== pointerId) return;
      const rect = widget.getBoundingClientRect();
      const pos = applyWidgetPosition(
        widget,
        ev.clientX - offsetX,
        ev.clientY - offsetY,
      );
      widget._sacPos = pos;
      ev.preventDefault();
    };

    const stopDrag = (ev) => {
      if (!dragging) return;
      if (ev && ev.pointerId !== pointerId) return;
      dragging = false;
      widget.classList.remove("sac-widget-dragging");
      header.releasePointerCapture?.(pointerId);
      pointerId = null;
      document.removeEventListener("pointermove", onPointerMove, true);
      document.removeEventListener("pointerup", stopDrag, true);
      document.removeEventListener("pointercancel", stopDrag, true);
      if (widget._sacPos) saveWidgetPosition(widget._sacPos.left, widget._sacPos.top);
    };

    header.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 || ev.target.closest(".sac-widget-close")) return;
      const rect = widget.getBoundingClientRect();
      applyWidgetPosition(widget, rect.left, rect.top);
      dragging = true;
      pointerId = ev.pointerId;
      offsetX = ev.clientX - rect.left;
      offsetY = ev.clientY - rect.top;
      widget.classList.add("sac-widget-dragging");
      header.setPointerCapture?.(ev.pointerId);
      document.addEventListener("pointermove", onPointerMove, true);
      document.addEventListener("pointerup", stopDrag, true);
      document.addEventListener("pointercancel", stopDrag, true);
      ev.preventDefault();
    });

    widget.querySelector(".sac-widget-close").addEventListener("click", (ev) => {
      ev.stopPropagation();
      chrome.runtime.sendMessage({ type: "TOGGLE_WIDGET" }).catch(() => {});
    });

    widget.querySelector(".sac-widget-toggle").addEventListener("click", () => {
      chrome.runtime.sendMessage({ type: "TOGGLE_FROM_WIDGET" }).catch(() => {});
    });

    window.addEventListener("resize", () => {
      const rect = widget.getBoundingClientRect();
      const pos = applyWidgetPosition(widget, rect.left, rect.top);
      widget._sacPos = pos;
      saveWidgetPosition(pos.left, pos.top);
    });
  }

  function createWidget() {
    const widget = document.createElement("div");
    widget.id = WIDGET_ID;
    widget.innerHTML = `
      <div class="sac-widget-header" title="Kéo thả để di chuyển">
        <span class="sac-widget-grip" aria-hidden="true">⠿</span>
        <span class="sac-widget-title">Auto Clicker</span>
        <button type="button" class="sac-widget-close" title="Ẩn widget">×</button>
      </div>
      <div class="sac-widget-body">
        <div class="sac-widget-status">
          <span class="sac-w-dot"></span>
          <span class="sac-w-text">Tạm dừng</span>
        </div>
        <div class="sac-widget-stats">
          <div class="sac-widget-stat">
            <span class="sac-w-clicks">0</span>
            <span class="sac-w-label">Click</span>
          </div>
          <div class="sac-widget-stat">
            <span class="sac-w-cycles">0</span>
            <span class="sac-w-label">Chu kỳ</span>
          </div>
          <div class="sac-widget-stat">
            <span class="sac-w-targets">0</span>
            <span class="sac-w-label">Mục tiêu</span>
          </div>
          <div class="sac-widget-stat">
            <span class="sac-w-time">00:00</span>
            <span class="sac-w-label">Thời gian</span>
          </div>
        </div>
        <button type="button" class="sac-widget-toggle">▶ Bắt đầu</button>
      </div>
    `;
    document.documentElement.appendChild(widget);
    bindWidgetDrag(widget);
    loadWidgetPosition((saved) => {
      if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
        widget._sacPos = applyWidgetPosition(widget, saved.left, saved.top);
      }
    });
    return widget;
  }

  function paintWidget(widget, data) {
    const running = !!data?.running;
    widget.querySelector(".sac-w-dot").classList.toggle("running", running);
    widget.querySelector(".sac-w-text").textContent = running ? "Đang chạy" : "Tạm dừng";
    widget.querySelector(".sac-w-clicks").textContent = String(data?.totalClicks ?? 0);
    widget.querySelector(".sac-w-cycles").textContent = String(data?.cycles ?? 0);
    widget.querySelector(".sac-w-targets").textContent = String(data?.targets ?? 0);
    widget.querySelector(".sac-w-time").textContent = data?.elapsed ?? "00:00";
    widget.querySelector(".sac-widget-toggle").textContent = running ? "⏸ Tạm dừng" : "▶ Bắt đầu";
    widget.classList.toggle("sac-widget-running", running);
  }

  function updateWidget(data) {
    let widget = document.getElementById(WIDGET_ID);
    if (!state.widgetVisible) {
      widget?.remove();
      return;
    }
    if (!widget) widget = createWidget();
    paintWidget(widget, data);
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    switch (msg.type) {
      case "PING":
        sendResponse({ ok: true });
        break;

      case "SYNC_STATE":
        state.points = msg.points || [];
        state.textTargets = msg.textTargets || [];
        state.showMarkers = msg.showMarkers !== false;
        state.capturing = !!msg.capturing;
        state.capturingText = !!msg.capturingText;
        state.widgetVisible = !!msg.widgetVisible;
        renderMarkers();
        renderTextHighlights();
        renderCaptureOverlay();
        updateWidget(msg.widgetData);
        sendResponse({ ok: true });
        break;

      case "SET_CAPTURE":
        state.capturing = !!msg.enabled;
        if (msg.enabled) state.capturingText = false;
        renderCaptureOverlay();
        sendResponse({ ok: true });
        break;

      case "SET_TEXT_CAPTURE":
        state.capturingText = !!msg.enabled;
        if (msg.enabled) state.capturing = false;
        renderCaptureOverlay();
        sendResponse({ ok: true });
        break;

      case "SET_POINTS":
        state.points = msg.points || [];
        renderMarkers();
        sendResponse({ ok: true });
        break;

      case "SET_TEXT_TARGETS":
        state.textTargets = msg.textTargets || [];
        renderTextHighlights();
        sendResponse({ ok: true });
        break;

      case "PERFORM_CLICK":
        sendResponse({ ok: performClick(msg.x, msg.y) });
        break;

      case "PERFORM_TEXT_CLICK": {
        const el = findElementForText(msg.text, msg.exact !== false);
        sendResponse({ ok: el ? clickElementCenter(el) : false });
        break;
      }

      case "WIDGET_UPDATE":
        updateWidget(msg.data);
        sendResponse({ ok: true });
        break;

      case "SET_WIDGET":
        state.widgetVisible = !!msg.visible;
        updateWidget(msg.data || {});
        sendResponse({ ok: true });
        break;

      default:
        break;
    }
    return true;
  });

  chrome.runtime.sendMessage({ type: "CONTENT_READY" }).catch(() => {});
})();
