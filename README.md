# TDTU Calendar & Telegram Bot

Tự động lấy lịch học, lịch thi và deadline eLearning từ cổng TDTU, sau đó đồng bộ trực tiếp vào Google Calendar. Telegram chỉ là giao diện xem và thêm lịch; Google Calendar là nơi lưu trữ duy nhất.

## Luồng hoạt động

```text
run_hour.py
  └─ crawler.py (TDTU / eLearning)
       └─ calendar_sync.py → Google Calendar
                               └─ Telegram bot / morning notification
```

- Lịch học dùng thẻ nội bộ `class_session`.
- Lịch thi dùng thẻ `exam` và tiêu đề `[EXAM]`.
- Deadline dùng thẻ `deadline` và tiêu đề `[DEADLINE]`.
- `/deadline` và `/exam` chỉ lấy các sự kiện từ ngày mai trở đi qua Google Calendar API.

## Telegram commands

| Lệnh | Chức năng |
| --- | --- |
| `/today` | Lịch hẹn trong ngày |
| `/schedule [hôm nay\|mai\|YYYY-MM-DD]` | Lịch học theo ngày |
| `/deadline` | Deadline eLearning sắp tới |
| `/exam` | Lịch thi trong 90 ngày tới |
| `/add` | Thêm lịch hẹn trực tiếp vào Google Calendar |

## Cài đặt local

1. Tạo môi trường Python và cài dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Sao chép cấu hình mẫu:

   ```bash
   cp .env.example .env
   ```

3. Điền ít nhất các biến sau trong `.env`:

   ```dotenv
   STUDENT_ID=...
   PASSWORD=...
   GOOGLE_CALENDAR_ID=your_calendar_id
   GOOGLE_SERVICE_ACCOUNT_FILE=service-account.json
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```

4. Chia sẻ Google Calendar đích cho `client_email` trong `service-account.json` với quyền **Make changes to events**. Không dùng `GOOGLE_CALENDAR_ID=primary`.

5. Chạy đồng bộ:

   ```bash
   python run_hour.py
   ```

6. Chạy webhook Telegram (môi trường production cần URL HTTPS công khai):

   ```bash
   uvicorn webhook_app:app --host 0.0.0.0 --port 8000
   ```

## GitHub Actions

Workflow `Hourly Schedule Sync` chạy:

- Mỗi giờ;
- Mỗi lần push lên nhánh `main`;
- Khi bấm **Run workflow** trên GitHub.

Thêm các GitHub secrets: `STUDENT_ID`, `PASSWORD`, `GOOGLE_CALENDAR_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Giá trị `GOOGLE_SERVICE_ACCOUNT_JSON` là toàn bộ nội dung JSON của service account, không phải đường dẫn tệp local.

Workflow `Daily Morning Notification` gửi tổng hợp lịch mỗi ngày và đọc trực tiếp từ Google Calendar.

## Kiểm tra

```bash
pytest -q
python -m py_compile *.py
```

## Ghi chú vận hành

- `service-account.json`, `.env`, và mọi khóa Telegram/Google không được commit.
- Nếu một crawler nguồn bị lỗi, `run_hour.py` không xóa dữ liệu Calendar hiện có của nguồn đó.
- Logs của `run_hour.py` và webhook nêu rõ từng bước crawl, đồng bộ và xử lý lệnh Telegram.
