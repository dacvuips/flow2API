# Speed Auto Clicker (Chrome Extension)

Extension tự động click nhiều điểm trên trang web, giao diện tối giống Speed Auto Clicker v3.0.

## Tab Clear Data (v3.3)

Tab **Clear Data** — xóa dữ liệu site theo chu kỳ (giống tab Refresh):

- Nút: **5s, 10s, 20s, 1p, 2p, 5p, 10p** + nhập giây tùy chỉnh
- **BẮT ĐẦU** — xóa ngay lần đầu, sau đó lặp theo interval
- Xóa: cookies, cache, localStorage, IndexedDB, service workers… của **domain tab hiện tại**
- Sau mỗi lần xóa → **reload** trang

Cần quyền `browsingData` (Chrome hỏi khi reload extension).

## Tab Auto Refresh

Chuyển sang tab **Auto Refresh** trong popup:

- Nút nhanh: **5s, 10s, 20s, 1p, 2p, 5p, 10p**
- Ô nhập **tùy chỉnh** (1–3600 giây)
- **BẮT ĐẦU** — refresh tab trang đang mở theo chu kỳ
- **DỪNG** — tắt auto refresh
- Hiển thị số lần refresh và đếm ngược lần tiếp theo

## Cài đặt

1. Mở Chrome → `chrome://extensions/`
2. Bật **Developer mode** (Chế độ nhà phát triển)
3. Chọn **Load unpacked** → chọn thư mục `extention`

## Click theo chữ (tab Click, v3.4)

1. Bấm **+ CHỮ** (`Alt+T`) → click vào chữ/nút trên trang để thêm vào danh sách TEXT
2. Mỗi dòng có **delay riêng** (giây) — tốc độ chờ sau mỗi lần click chữ đó
3. Chọn chế độ: **Tọa độ** | **Chữ** | **Cả hai** (lần lượt điểm rồi chữ)
4. **RESUME** để chạy — extension tìm element có đúng chữ và click vào giữa

## Cách dùng

1. Mở trang web cần auto click
2. Bấm **+ ADD POINTS** (hoặc `Alt+X`), click từng vị trí trên trang
3. Nhấn `Esc` hoặc bấm lại **+ ADD POINTS** để thoát chế độ thêm điểm
4. Chỉnh **DEFAULT DELAY** (giây mặc định khi thêm point mới)
5. Trong danh sách **POINTS**, chỉnh **delay riêng** từng point (− / +) — thời gian chờ **sau khi click** point đó
6. Bấm **RESUME** (hoặc `Alt+C`) để chạy, **PAUSE** để dừng

## Tính năng

| Tính năng | Mô tả |
|-----------|--------|
| Nhiều điểm click | Lưu tọa độ (x, y) theo viewport |
| Delay từng point | Mỗi point có giây chờ riêng sau khi click (± / − trên từng dòng) |
| Default delay | Giá trị mặc định cho point mới; nút **↻ All** gán cho tất cả |
| Humanize ±10% | Random hóa thời gian chờ |
| Chu kỳ (Cycles) | Trong **Settings**: `Max cycles` — `0` = vô hạn |
| Thống kê | Points, Cycles, Clicks, Time |
| Icon crosshair tím | Marker trên trang + danh sách points |
| Widget nổi | Nút **Widget** — panel kéo thả trên trang |
| Phím tắt | `Alt+X` thêm điểm, `Alt+C` start/pause |
| Lưu tự động | Trạng thái lưu trong `chrome.storage` |

## Settings (⚙)

- **Max cycles**: Số vòng lặp qua toàn bộ danh sách point (`0` = không giới hạn)
- **Interval step**: Bước tăng/giảm interval
- **Show crosshair markers**: Hiện icon tím trên trang
- **Click through all points**: Mỗi chu kỳ click lần lượt tất cả point

## Widget trên trang (kéo thả)

1. Mở popup → bấm nút **Widget** (hoặc bật lại sau khi đã ẩn).
2. Widget nổi xuất hiện góc phải trang — **kéo thanh tiêu đề** `⠿ Auto Clicker` để di chuyển.
3. Vị trí được **lưu tự động** (dùng lại ở tab/trang khác).
4. Nút **×** ẩn widget; **▶ Bắt đầu / ⏸ Tạm dừng** điều khiển clicker không cần mở popup.

Widget hiển thị: trạng thái, số click, chu kỳ, mục tiêu, thời gian chạy.

## Lưu ý

- Tọa độ theo **viewport** (cửa sổ hiện tại). Cuộn trang có thể làm lệch vị trí — nên thêm point khi trang ở vị trí bạn sẽ dùng.
- Một số trang (chrome://, Chrome Web Store) không cho phép content script.
- Chỉ dùng trên trang bạn được phép tương tác tự động.

## Cấu trúc

```
manifest.json   — Manifest V3
popup.html/css/js — Giao diện extension
background.js   — Logic auto click, timer, lưu state
content.js/css  — Marker, click, widget trên trang
icons/          — Icon extension
```
