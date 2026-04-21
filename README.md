# tool-check-tkb

Bot tự động lấy thời khóa biểu TDTU, đồng bộ vào Supabase, và gửi thông báo lịch học/lịch hẹn qua Telegram.

README này dành cho người mới: đọc xong có thể chạy local, hiểu các script chính, và cấu hình GitHub Actions đúng cách.

## 1) Repo này làm gì?

Có 3 luồng chính:

1. Thu thập dữ liệu (`run_hour.py`)
   - Crawl lịch học từ portal TDTU
   - Ghi vào Supabase
   - Sync Google Calendar (nếu cấu hình)

2. Gửi bản tin buổi sáng (`main.py`)
   - Lấy lịch hôm nay từ Supabase
   - Gửi Telegram summary

3. Nhận lịch hẹn từ Telegram (`telegram_mvp_bot.py` hoặc `webhook_app.py`)
   - Parse tin nhắn người dùng
   - Lưu appointment vào Supabase

## 2) Cần chuẩn bị gì trước khi chạy?

- Python 3.11
- Tài khoản Supabase (đã tạo project)
- Telegram bot token (từ BotFather)
- Tài khoản TDTU: `STUDENT_ID` + `PASSWORD`
- Playwright Chromium (để crawl portal)

## 3) Setup local nhanh (khuyên dùng cho new user)

### Bước 1: Cài dependency

```bash
cd /home/tuananh/Documents/tool-check-tkb
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Bước 2: Tạo file môi trường

```bash
cp .env.example .env
```

Mở `.env` và điền tối thiểu các biến sau:

- `STUDENT_ID`
- `PASSWORD`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Bước 3: Tạo schema Supabase

Chạy SQL trong file `supabase/init_tables.sql` bằng Supabase SQL Editor.

### Bước 4: Test chạy end-to-end

```bash
python run_hour.py
python main.py
```

Kỳ vọng:

- `run_hour.py` crawl + upsert dữ liệu thành công
- `main.py` gửi Telegram bản tin hôm nay

## 4) Các script chính và khi nào dùng

### `run_hour.py`

Dùng để đồng bộ dữ liệu định kỳ (crawl + DB + calendar).

```bash
python run_hour.py
```

### `main.py`

Dùng để gửi thông báo lịch hôm nay.

```bash
python main.py
```

### `telegram_mvp_bot.py` (long polling)

Phù hợp khi chạy local nhanh để test bot nhập lịch hẹn.

```bash
python telegram_mvp_bot.py
```

### `webhook_app.py` (FastAPI webhook)

Phù hợp cho production (Railway/VPS), Telegram gọi webhook qua HTTPS.

```bash
uvicorn webhook_app:app --host 0.0.0.0 --port 8000
```

Health check: `GET /health`

Webhook endpoint: `POST /telegram/webhook`

## 5) Format tạo lịch hẹn qua Telegram

Format mặc định:

```text
tieude-thoigian-diadiem(optional)
```

Ví dụ:

- `hop nhom-15/04 14:00-B402`
- `di kham-2026-04-16 09:30`
- `gym-18:00`

Lệnh hỗ trợ:

- `/help`
- `/today`

Nếu có `GEMINI_API_KEY`, bot ưu tiên parse ngôn ngữ tự nhiên bằng Gemini, sau đó mới fallback rule-based.

## 6) Biến môi trường quan trọng

Tham khảo đầy đủ trong `.env.example`.

Biến thường dùng nhất:

- `TARGET_SEMESTER`
  - Ví dụ: `HK2/2025-2026`
  - Nếu để trống, hệ thống tự chọn theo tháng hiện tại

- `CRAWLER_WEEKS_AHEAD`
  - Số tuần tương lai crawl thêm
  - Mặc định: `2`

- `APP_TIMEZONE`
  - Mặc định: `Asia/Ho_Chi_Minh`

- `GOOGLE_CALENDAR_ID`
- `GOOGLE_SERVICE_ACCOUNT_FILE` (local)
- `GOOGLE_SERVICE_ACCOUNT_JSON` (CI/GitHub Actions)

## 7) Chạy bằng GitHub Actions

Repo có 2 workflow:

- `.github/workflows/hourly_sync.yml`
  - Chạy mỗi giờ để crawl + sync dữ liệu

- `.github/workflows/daily_tkb.yml`
  - Chạy mỗi ngày để gửi bản tin sáng

### Secrets cần có trên GitHub

- `STUDENT_ID`
- `PASSWORD`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GOOGLE_CALENDAR_ID`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GOOGLE_CALENDAR_REQUIRED`

Lưu ý quan trọng:

- Trên CI, dùng `GOOGLE_SERVICE_ACCOUNT_JSON` (raw JSON), không dùng đường dẫn file local.
- `SUPABASE_SERVICE_ROLE_KEY` nên luôn có để tránh lỗi quyền khi đọc/ghi do RLS.

## 8) Troubleshooting nhanh

### Lỗi thiếu credentials TDTU

`Crawler failed: Credentials missing`

Kiểm tra lại `STUDENT_ID`, `PASSWORD` trong `.env` hoặc GitHub Secrets.

### Lỗi Supabase RLS / 42501

Đảm bảo `SUPABASE_SERVICE_ROLE_KEY` đúng project và còn hiệu lực.

### Không gửi được Telegram

Kiểm tra `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

### Action chạy nhưng không có dữ liệu hôm nay

Kiểm tra:

- workflow hourly có chạy trước daily không
- `STUDENT_ID` ở local và GitHub Secrets có giống nhau không
- timezone (`APP_TIMEZONE`) có đúng không

## 9) Bảo mật và Git

- Không commit `.env`.
- File `.env.example` dùng để chia sẻ template, không chứa secret thật.
- Nếu lỡ track `.env` trước đó:

```bash
git rm --cached .env
git commit -m "stop tracking .env"
```

## 10) Tài liệu liên quan

- `QUICKSTART.md`
- `LOCAL_SETUP.md`
- `DEPLOY_RAILWAY.md`
- `SYSTEMD_AUTORUN.md`
- `supabase/init_tables.sql`

---

Nếu bạn là new user, chỉ cần làm theo mục 3 là có thể chạy thử thành công trong 10-15 phút.
