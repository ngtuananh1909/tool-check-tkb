# Local Development Setup

This guide helps you run the webhook server locally for testing.

## 1. Clone & Install Dependencies

```bash
cd /home/tuananh/Documents/tool-check-tkb

# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required for TDTU portal scraping)
playwright install chromium
```

## 2. Configure Environment

```bash
# Copy template and fill in your credentials
cp .env.example .env

# Edit .env with your values:
nano .env
```

**Required fields:**
- `STUDENT_ID` → Your TDTU student ID
- `PASSWORD` → Your TDTU portal password
- `SUPABASE_URL` → Your Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` → Supabase service role key (for writes)
- `SUPABASE_KEY` → Supabase public key (for reads)
- `TELEGRAM_BOT_TOKEN` → Your Telegram bot token (from @BotFather)
- `TELEGRAM_CHAT_ID` → Your Telegram chat ID (get from bot: send `/start` to your bot, check logs)

**Optional:**
- `GEMINI_API_KEY` → Google Gemini API key (for NLP parsing)
- `TELEGRAM_WEBHOOK_URL` → Leave empty for local testing
- `TELEGRAM_WEBHOOK_SECRET` → Leave empty for local testing

## 3. Test Supabase Connection

```bash
python -c "
import os
from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

client = create_client(
    os.environ['SUPABASE_URL'],
    os.environ['SUPABASE_KEY']
)
print('✓ Supabase connection OK')
"
```

## 4. Run Webhook Server Locally

```bash
# Start webhook server on http://localhost:8000
uvicorn webhook_app:app --reload

# Test health endpoint in another terminal:
curl http://localhost:8000/health
# Should return: {"status":"ok"}
```

## 5. Test Appointment Creation via Telegram

```bash
# Send message to your Telegram bot:
hop nhom-15/04 14:00-B402

# Or use /today to list today's appointments:
/today
```

## 6. Run Daily Schedule Notification (GitHub Actions locally)

```bash
python main.py
# Fetches TDTU schedule and sends to Telegram
```

## 7. Troubleshooting

### "Missing TELEGRAM_BOT_TOKEN"
→ Check `.env` file has `TELEGRAM_BOT_TOKEN=your_actual_token`

### "No module named 'google'"
→ Install Gemini SDK: `pip install google-generativeai`

### Playwright timeout on TDTU portal
→ Check internet connection; portal may be down

### Database permissions error
→ Ensure `SUPABASE_SERVICE_ROLE_KEY` is used (not anon key)

## Next Steps

- **For 24/7 auto-run on Railway**: See [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)
- **For systemd auto-run on your machine**: See [SYSTEMD_AUTORUN.md](SYSTEMD_AUTORUN.md)
