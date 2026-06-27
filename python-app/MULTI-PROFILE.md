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
- Retry reCAPTCHA: tối đa 2 lần trên cùng profile (SDK), lần 3 **đổi profile** — lặp đổi profile **đến khi thành công** (không giới hạn số lần ở worker)
- Upsample video/ảnh: retry reCAPTCHA **chỉ trên profile gốc**, cũng **không giới hạn** số lần requeue
- Extension: reload tab Flow trước mỗi lần giải captcha sau N lần (mặc định N=1) để token reCAPTCHA luôn mới
- Profile tắt Chrome → task mới chuyển sang profile khác; task đang chạy trên profile đó có thể lỗi
