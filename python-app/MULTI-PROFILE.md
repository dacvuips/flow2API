# Multi Chrome Profile (10+ tài khoản Veo)

## Cách hoạt động

- **Một extension** (cùng folder `extension/`) cài trên **mỗi Chrome profile**
- Tất cả profile kết nối **cùng agent** `ws://127.0.0.1:1609`
- Agent **phân bổ task round-robin** giữa các profile có token Flow
- **Poll video tự động** — không cần thao tác thủ công
- Mỗi task ghi `profile_email` / `profile_label` — xem cột **Profile** trên Dashboard

## Thiết lập (mỗi profile, ~2 phút)

1. Mở Chrome profile (Person 1, Person 2, …)
2. `chrome://extensions` → Bật Developer mode → **Load unpacked** → chọn folder `extension/`
3. Đăng nhập Google / Veo khác nhau trên từng profile
4. Mở tab: https://labs.google/fx/tools/flow
5. Bấm icon extension → thấy **đã kết nối** + email tài khoản

Lặp lại cho 10 profile. **Không cần** cấu hình port/ID khác nhau — mỗi profile tự có `profileId` trong storage.

## Agent

```bat
cd python-app
run.bat
```

Tab **Tasks** → thanh **Chrome profiles** hiển thị profile online.  
Gợi ý queue:
- **Tổng** = tổng task chạy cùng lúc (vd 10–20)
- **Profile mặc định** = song song mặc định mỗi profile (vd 1)
- **Song song** (từng profile trên thanh profiles) = cấu hình riêng, vd profile mạnh = 2, profile yếu = 1
- **Cách** = 2–5 giây giữa mỗi lần start task

## Lưu ý

- Một task luôn chạy trên **một profile** từ đầu đến cuối (upload + generate + poll)
- Retry reCAPTCHA giữ nguyên profile + media đã upload (tối đa 10 lần/profile ở SDK; worker requeue tối đa 10 lần, chờ 3–5s, đổi profile mỗi lần requeue)
- **Settings** tab trên Dashboard: Telegram, proxy pool, điều khiển hệ thống (F5/Launch/Close Chrome), khởi động cùng Windows
- Flow URL mặc định: `https://labs.google/fx/vi/tools/flow`
- Profile tắt Chrome → task mới chuyển sang profile khác; task đang chạy trên profile đó có thể lỗi

## Captcha Center (reCAPTCHA tập trung)

Worker profile **không** tự mint captcha trên tab Flow của mình. Token lấy từ **profile Center** riêng qua agent broker.

### Tại sao tách Center?

- reCAPTCHA Enterprise gắn score theo session/cookie trên tab Flow
- Nhiều profile worker cùng mint → score giảm nhanh
- 1–N profile Center chuyên mint, worker chỉ inject token → ổn định hơn

### Thiết lập nhanh

1. **1+ Chrome profile Center** — cùng extension, popup → **Captcha Center** → label `captcha-01`, …
2. Tab Flow mở + đăng nhập Google trên mỗi profile Center
3. **N profile worker** — popup → **Bridge**, đăng nhập Veo khác nhau
4. Agent `run.bat` — secret broker: `python-app/storage/captcha-center.secret`

### Luồng request

```
Worker Bridge  →  agent.request_captcha(action)  →  broker chọn Center (LRU)
Center mint    →  POST /result { commandId, token }
Agent gửi WS   →  api_request { captchaToken }  →  Bridge inject vào body → Google API
```

`commandId` UUID đảm bảo token về đúng request/profile worker.

### Nhiều Center / chuyển đổi

- Broker tự chọn center online, không cooldown, LRU (center lâu chưa mint)
- API 403 từ Google → broker enqueue **hard_reset** (xóa cookie anchor + reload tab Flow)
- Hard reset định kỳ: 20 solve hoặc 10 phút/center
- Stats: Dashboard hoặc `GET http://127.0.0.1:1994/api/internal/captcha/stats`

### Timing (từ veo3-captcha-extension)

| Tham số | Giá trị |
|---------|---------|
| Long-poll | 25s |
| Mint lần 1 / retry | 25s / 20s |
| Hard reset blank + dwell | 1.5s + 2.5s |
| Heartbeat keepalive SW | ~24s |
| Center stale (offline) | 45s không heartbeat |
| Cooldown sau hard_reset | 5s |
| Broker timeout/request | 30s |

Chi tiết: `extension/README.md`
