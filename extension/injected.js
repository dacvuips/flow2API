/**
 * Injected into MAIN world on labs.google — has access to window.grecaptcha.
 * Used solely for reCAPTCHA solving. Media URLs come from the generation API
 * response directly (agent extracts fifeUrl from data.media[].image), so no
 * TRPC response interception is needed.
 */
(() => {
  if (window.__flow2apiCaptchaBridge) return;
  window.__flow2apiCaptchaBridge = true;
  document.documentElement.dataset.flow2apiMainReady = "1";

  const SITE_KEY_FALLBACK = "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV";

  /** Read site key from Flow page (enterprise.js?render=KEY) — avoids stale hardcoded keys. */
  function resolveSiteKey() {
    for (const script of document.scripts) {
      const src = script.src || "";
      const fromRender = src.match(/recaptcha\/enterprise\.js\?render=([^&]+)/);
      if (fromRender?.[1]) return fromRender[1];
    }
    const el = document.querySelector("[data-sitekey]");
    if (el?.dataset?.sitekey) return el.dataset.sitekey;
    return SITE_KEY_FALLBACK;
  }

  let _captchaInflight = null;

  async function executeCaptcha(pageAction) {
    if (_captchaInflight) return _captchaInflight;
    _captchaInflight = (async () => {
      await waitForGrecaptcha();
      const siteKey = resolveSiteKey();
      return await new Promise((resolve, reject) => {
        window.grecaptcha.enterprise.ready(async () => {
          try {
            resolve(
              await window.grecaptcha.enterprise.execute(siteKey, {
                action: pageAction,
              }),
            );
          } catch (e) {
            reject(e);
          }
        });
      });
    })();
    try {
      return await _captchaInflight;
    } finally {
      _captchaInflight = null;
    }
  }

  document.addEventListener("GET_CAPTCHA", async ({ detail }) => {
    const { requestId, pageAction } = detail;
    try {
      const token = await executeCaptcha(pageAction);
      document.dispatchEvent(
        new CustomEvent("CAPTCHA_RESULT", {
          detail: { requestId, token },
        }),
      );
    } catch (e) {
      document.dispatchEvent(
        new CustomEvent("CAPTCHA_RESULT", {
          detail: { requestId, error: e.message },
        }),
      );
    }
  });

  function waitForGrecaptcha(timeout = 15000) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const check = () => {
        if (window.grecaptcha?.enterprise?.execute) return resolve();
        if (Date.now() - start > timeout)
          return reject(new Error("grecaptcha not available"));
        setTimeout(check, 200);
      };
      check();
    });
  }

  const AUTO_CLICK_BTN = "create with google flow";

  function normalizeBtnText(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase();
  }

  function isVisible(el) {
    if (!el || el.closest?.("[hidden]")) return false;
    if (el.disabled === true) return false;
    const rect = el.getBoundingClientRect?.();
    if (!rect || rect.width <= 0 || rect.height <= 0) return false;
    const style = window.getComputedStyle(el);
    return (
      style.visibility !== "hidden" &&
      style.display !== "none" &&
      style.pointerEvents !== "none" &&
      Number(style.opacity || 1) > 0.01
    );
  }

  function spanLabelMatchesCreateFlow(el) {
    return normalizeBtnText(el?.textContent) === AUTO_CLICK_BTN;
  }

  /** Primary: <button><span>Create with Google Flow</span></button> (Google Flow landing). */
  function findCreateFlowButtonExact(root = document) {
    let found = null;
    walkShadowRoots(root, (scope) => {
      if (found) return;
      scope.querySelectorAll?.("button").forEach((btn) => {
        if (found || !isVisible(btn)) return;
        const spans = btn.querySelectorAll("span");
        for (const span of spans) {
          if (spanLabelMatchesCreateFlow(span)) {
            found = btn;
            return;
          }
        }
        if (spanLabelMatchesCreateFlow(btn)) found = btn;
      });
    });
    return found;
  }

  function elementMatchesCreateFlow(el) {
    const text = normalizeBtnText(el.textContent);
    const aria = normalizeBtnText(el.getAttribute?.("aria-label"));
    const title = normalizeBtnText(el.getAttribute?.("title"));
    const haystack = `${text} ${aria} ${title}`;
    if (!haystack.includes(AUTO_CLICK_BTN)) return false;
    if (text.length > 80 && !text.includes(AUTO_CLICK_BTN)) return false;
    return isVisible(el);
  }

  function isClickableTag(el) {
    const tag = (el.tagName || "").toLowerCase();
    if (tag === "button" || tag === "a") return true;
    const role = (el.getAttribute?.("role") || "").toLowerCase();
    if (role === "button" || role === "link") return true;
    if (typeof el.onclick === "function") return true;
    return false;
  }

  function scoreCreateFlowCandidate(el) {
    const text = normalizeBtnText(el.textContent);
    let score = text.length;
    if (isClickableTag(el)) score -= 100;
    if (text === AUTO_CLICK_BTN) score -= 200;
    if (text.includes(AUTO_CLICK_BTN) && text.length < 40) score -= 50;
    return score;
  }

  function walkShadowRoots(root, visit) {
    if (!root) return;
    visit(root);
    root.querySelectorAll?.("*").forEach((el) => {
      if (el.shadowRoot) walkShadowRoots(el.shadowRoot, visit);
    });
  }

  function findCreateFlowButton() {
    const exact = findCreateFlowButtonExact();
    if (exact) return exact;

    let best = null;
    let bestScore = Infinity;
    walkShadowRoots(document, (root) => {
      root.querySelectorAll?.(
        "button, a, [role='button'], [role='link'], input[type='button'], input[type='submit']",
      ).forEach((el) => {
        if (!elementMatchesCreateFlow(el)) return;
        const score = scoreCreateFlowCandidate(el);
        if (score < bestScore) {
          bestScore = score;
          best = el;
        }
      });
    });
    return resolveClickTarget(best);
  }

  function resolveClickTarget(el) {
    if (!el) return null;
    let node = el;
    for (let i = 0; i < 5 && node; i += 1) {
      if (isClickableTag(node)) return node;
      node = node.parentElement;
    }
    return el;
  }

  function triggerCreateFlowClick(el) {
    const target = resolveClickTarget(el) || el;
    const innerSpan = target.querySelector?.("span");
    const clickNodes = innerSpan && spanLabelMatchesCreateFlow(innerSpan)
      ? [target, innerSpan]
      : [target];
    for (const node of clickNodes) {
      try {
        node.scrollIntoView({ block: "center", inline: "center", behavior: "auto" });
      } catch {
        /* ignore */
      }
      try {
        node.focus({ preventScroll: true });
      } catch {
        /* ignore */
      }
      for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
        node.dispatchEvent(
          new MouseEvent(type, { bubbles: true, cancelable: true, view: window }),
        );
      }
      if (typeof node.click === "function") node.click();
    }
  }

  document.addEventListener("FLOW2API_TRY_AUTO_CLICK_CREATE", ({ detail }) => {
    const requestId = detail?.requestId || "";
    try {
      const btn = findCreateFlowButton();
      if (!btn) {
        document.dispatchEvent(
          new CustomEvent("FLOW2API_AUTO_CLICK_RESULT", {
            detail: { requestId, clicked: false, reason: "button_not_found" },
          }),
        );
        return;
      }
      triggerCreateFlowClick(btn);
      document.dispatchEvent(
        new CustomEvent("FLOW2API_AUTO_CLICK_RESULT", {
          detail: {
            requestId,
            clicked: true,
            tag: btn.tagName,
            text: normalizeBtnText(btn.textContent).slice(0, 80),
          },
        }),
      );
    } catch (e) {
      document.dispatchEvent(
        new CustomEvent("FLOW2API_AUTO_CLICK_RESULT", {
          detail: {
            requestId,
            clicked: false,
            reason: "error",
            error: e?.message || String(e),
          },
        }),
      );
    }
  });
})();

