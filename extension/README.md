# Flow2API Bridge (Chrome MV3)

Extension cục bộ — proxy request Flow qua agent Python. Hỗ trợ **2 mode** trong cùng 1 package (chọn ở popup):

| Mode | Chrome profile | Vai trò |
|------|----------------|---------|
| **Bridge** (mặc định) | Profile worker (đăng nhập Veo) | Bắt Bearer token, proxy API, **inject** `captchaToken` do Center cấp |
| **Captcha Center** | Profile riêng (1 hoặc nhiều) | Mint reCAPTCHA trên tab Flow, long-poll agent broker |

Worker **không** tự gọi `grecaptcha` trên tab của mình — tránh burn score trên nhiều profile.

## Cài đặt

1. Mở `chrome://extensions` → bật **Developer mode** → **Load unpacked** → chọn folder `extension/`.
2. Lặp lại trên mỗi Chrome profile (worker + center).

## Kiến trúc Captcha Center

```
Bridge profile (worker)          Python agent (127.0.0.1:1994)
       │                              │
       │  WS api_request              │  CaptchaBroker.request_captcha()
       │  + captchaToken (inject)     │       │
       │◄─────────────────────────────│       │ long-poll GET /poll
       │                              │       ▼
       │                              │  Captcha Center profile(s)
       │                              │  grecaptcha.enterprise.execute()
```

- Mỗi request có `commandId` (UUID) — token trả đúng Bridge profile đã gọi, không lẫn.
- Nhiều Center: broker chọn **LRU** (center lâu chưa mint), bỏ qua center đang cooldown/hard_reset.
- Timing (parity `veo3-captcha-extension`):
  - Long-poll: **25s**
  - Mint timeout: **25s** + retry **20s** (reinject content script)
  - Hard reset: xóa anchor cookie → `about:blank` **1.5s** → reload Flow → dwell **2.5s**
  - Hard reset định kỳ: mỗi **20** solve **hoặc** **10 phút**
  - Cooldown sau reset: **5s**
  - Center offline nếu không heartbeat **45s**
  - Broker request timeout: **30s**

## Thiết lập Captcha Center (profile riêng)

1. Chrome profile mới (không cần là profile worker).
2. Load cùng extension → popup → chọn **Captcha Center** → **Áp dụng** (extension reload).
3. Mở tab `https://labs.google/fx/tools/flow` — đăng nhập Google (account dùng mint captcha).
4. Popup Center:
   - **Bridge URL**: `http://127.0.0.1:1994` (agent loopback)
   - **Secret**: bấm **Test** để auto-fetch từ agent, hoặc copy từ `python-app/storage/captcha-center.secret`
   - **Label**: vd `captcha-01`, `captcha-02` (phân biệt nhiều center)
5. Badge icon **C** xanh dương = đang poll. Xem stats: popup hoặc `GET /api/internal/captcha/stats`.

Có thể chạy **nhiều profile Center** (mỗi profile 1 tab Flow). Giữ **1 tab Flow** / profile Center.

Agent **gắn cố định** Center ↔ Bridge (sort theo label/id, không chồng chéo):
- 3 Center + 3 Bridge → 1:1
- 3 Center + 2 Bridge → C1↔B1, C2↔B2, C3↔B2
- Xem trên Dashboard (Tasks → Profiles) hoặc `/api/internal/captcha/stats` field `pairings`

## Thiết lập Bridge (profile worker)

1. Popup → mode **Bridge** (mặc định).
2. Đăng nhập Veo trên tab Flow.
3. Agent chạy (`run.bat`) — extension kết nối `ws://127.0.0.1:1609`.
4. Khi generate cần captcha, agent xin token từ broker → gửi `captchaToken` qua WS → Bridge inject vào body trước khi gọi Google API.

Nếu không có Center online → lỗi `NO_CAPTCHA_CENTER` (không fallback self-solve).

## Content script + injected script

`content.js` inject `injected.js` (MAIN world) để gọi `grecaptcha.enterprise.execute`.  
Chỉ **Captcha Center** dùng luồng này qua long-poll. Bridge chỉ inject token sẵn có.

## Popup

Connection status, token age, mode Bridge/Center, cấu hình Center, số center online.
