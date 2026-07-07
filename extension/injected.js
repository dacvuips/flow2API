/**
 * Injected into MAIN world on labs.google — has access to window.grecaptcha.
 * Used solely for reCAPTCHA solving. Media URLs come from the generation API
 * response directly (agent extracts fifeUrl from data.media[].image), so no
 * TRPC response interception is needed.
 */
(() => {
  if (window.__flow2apiCaptchaBridge) return;
  window.__flow2apiCaptchaBridge = true;

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

  /** Strip `_grecaptcha*` localStorage pre/post mint — tránh marker tích lũy (hạ score). */
  function clearGrecaptchaCache() {
    try {
      Object.keys(localStorage)
        .filter((k) => k.startsWith("_grecaptcha"))
        .forEach((k) => localStorage.removeItem(k));
    } catch {
      /* cross-origin storage denied */
    }
  }

  async function executeCaptcha(pageAction) {
    if (_captchaInflight) return _captchaInflight;
    _captchaInflight = (async () => {
      await waitForGrecaptcha();
      clearGrecaptchaCache();
      const siteKey = resolveSiteKey();
      const token = await new Promise((resolve, reject) => {
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
      clearGrecaptchaCache();
      return token;
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
})();
