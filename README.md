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
- `CRAWLER_WEEKS_AHEAD`: số tuần tương lai crawl thêm từ portal mỗi lần chạy. Mặc định `2` (tức là crawl tuần hiện tại + 2 tuần tới)
- `APP_TIMEZONE`: múi giờ để tính "hôm nay", mặc định `Asia/Ho_Chi_Minh`

Nếu không set `TARGET_SEMESTER`, bot tự chọn theo rule:

- Tháng 1-7: chọn `HK2/(năm trước)-(năm hiện tại)`
- Tháng 8-12: chọn `HK1/(năm hiện tại)-(năm sau)`

### 3. Chạy bot

```bash
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python main.py
```

### 4. Bật bảng lịch hẹn cá nhân trên Supabase (mới)

Để dùng tính năng lịch hẹn cá nhân và bản tin tổng hợp, chạy SQL trong file sau bằng Supabase SQL Editor:

- [supabase/init_tables.sql](supabase/init_tables.sql)

Sau khi chạy script này, bot sẽ có thêm bảng:

- `appointments`
- `notification_log`

Nếu chưa chạy SQL, bot vẫn gửi lịch học bình thường nhưng phần lịch hẹn sẽ rỗng.

### 5. Chạy bot Telegram tạo lịch hẹn (MVP)

Chạy listener Telegram (long polling):

```bash
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python telegram_mvp_bot.py
```

Bot sẽ ưu tiên dùng Gemini để đọc tin nhắn và tạo JSON trước, rồi mới fallback sang parser rule-based nếu Gemini chưa có hoặc trả kết quả không rõ.

### 6. Chạy webhook Telegram

Webhook là cách phù hợp nếu bạn muốn bot chạy khi tắt máy. Chạy local bằng FastAPI + uvicorn:

```bash
/home/tuananh/Documents/tool-check-tkb/.venv/bin/python -m uvicorn webhook_app:app --host 0.0.0.0 --port 8000
```

Muốn Telegram gọi được webhook, bạn cần:

- Một URL public HTTPS trỏ tới `/telegram/webhook`
- Điền `TELEGRAM_WEBHOOK_URL` và `TELEGRAM_WEBHOOK_SECRET` trong `.env`
- Khi app khởi động, nó sẽ tự đăng ký webhook nếu thấy `TELEGRAM_WEBHOOK_URL`

Để auto-run 24/7 trên VPS, dùng file service mẫu:

- [deploy/telegram-webhook.service](deploy/telegram-webhook.service)

Format tin nhắn để tạo lịch hẹn:

```text
tieude-thoigian-diadiem(optional)
```

Ví dụ hợp lệ:

- `hop nhom-15/04 14:00-B402`
- `di kham-2026-04-16 09:30`
- `gym-18:00`

Rule parse thời gian MVP:

- `YYYY-MM-DD HH:MM`
- `DD/MM/YYYY HH:MM`
- `DD/MM HH:MM` (tự dùng năm hiện tại)
- `HH:MM` (tự dùng ngày hôm nay)

Lệnh hỗ trợ nhanh:

- `/help`: xem hướng dẫn format
- `/today`: xem lịch hẹn hôm nay

Nếu có `GEMINI_API_KEY` trong `.env`, bot sẽ cố gắng hiểu câu tự nhiên và lưu vào database theo JSON. Nếu không có key, bot vẫn chạy bằng format cứng ở trên.

## Bot làm gì

Luồng chạy của bot:

1. Đăng nhập portal TDTU.
2. Mở trang thời khóa biểu.
3. Chọn đúng học kỳ và chế độ xem theo tuần.
4. Parse lịch học tuần hiện tại và các tuần kế tiếp theo `CRAWLER_WEEKS_AHEAD`.
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
