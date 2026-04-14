# tool-check-tkb

Bot này đăng nhập TDTU, lấy thời khóa biểu theo học kỳ/tuần, lưu vào Supabase, rồi gửi lịch hôm nay qua Telegram.

## Cách chạy end to end

### 1. Chuẩn bị môi trường

Từ thư mục project, cài dependency và Playwright browser:

```bash
cd /home/tuananh/Documents/tool-check-tkb
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python -m pip install -r requirements.txt
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python -m playwright install chromium
```

### 2. Tạo file `.env`

Copy từ `.env.example` rồi điền các giá trị thật.

Các biến cần có:

- `STUDENT_ID`: MSSV TDTU
- `PASSWORD`: mật khẩu portal
- `SUPABASE_URL`: URL project Supabase
- `SUPABASE_SERVICE_ROLE_KEY`: key có quyền ghi dữ liệu vào bảng `schedules`
- `TELEGRAM_BOT_TOKEN`: token bot Telegram
- `TELEGRAM_CHAT_ID`: chat id nhận thông báo

Tuỳ chọn:

- `TARGET_SEMESTER`: ép bot chọn học kỳ cụ thể, ví dụ `HK2/2025-2026`

Nếu không set `TARGET_SEMESTER`, bot tự chọn theo rule:

- Tháng 1-7: chọn `HK2/(năm trước)-(năm hiện tại)`
- Tháng 8-12: chọn `HK1/(năm hiện tại)-(năm sau)`

### 3. Chạy bot

```bash
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python main.py
```

## Bot làm gì

Luồng chạy của bot:

1. Đăng nhập portal TDTU.
2. Mở trang thời khóa biểu.
3. Chọn đúng học kỳ và chế độ xem theo tuần.
4. Parse lịch học tuần hiện tại.
5. Ghi dữ liệu vào Supabase.
6. Lấy lịch của hôm nay.
7. Gửi thông báo Telegram.

## Nếu muốn đổi học kỳ

Đặt trong `.env`:

```env
TARGET_SEMESTER=HK2/2025-2026
```

Sau đó chạy lại `main.py`.

## Lỗi thường gặp

### 1. `Crawler failed: Credentials missing`

Kiểm tra lại `STUDENT_ID` và `PASSWORD` trong `.env`.

### 2. `new row violates row-level security policy for table "schedules"`

Bot đang ghi Supabase bằng `SUPABASE_SERVICE_ROLE_KEY`. Nếu vẫn lỗi, kiểm tra:

- key có đúng là service role key không
- URL Supabase có đúng project không
- bảng `schedules` có RLS/policy phù hợp không

### 3. Bot lấy sai học kỳ

Đặt `TARGET_SEMESTER=HK2/2025-2026` trong `.env` để ép đúng học kỳ cần lấy.

### 4. Không gửi được Telegram

Kiểm tra `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID`.

## Ghi chú kỹ thuật

- Trang TKB TDTU dùng layout theo tuần dạng ma trận `Period x Day`, nên parser không phải bảng đơn giản.
- Học kỳ đúng được chọn từ dropdown trên trang TKB, và bot có thể tự chuyển sang chế độ xem tuần trước khi parse.