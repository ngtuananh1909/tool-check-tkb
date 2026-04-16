# Deploy to Railway (Recommended - Free Tier)

Railway is **free** and perfect for this use case. Your bot will be **always-on** and auto-deploy when you push to GitHub.

## Why Railway?

✅ **Always-on** → No 15-min sleep like Render  
✅ **Free tier** → $5/month free credits (plenty for this app)  
✅ **Auto-deploy** → GitHub integration (deploy on push)  
✅ **PostgreSQL** → Supabase already hosted externally  
✅ **Environment vars** → UI-based secret management  

---

## Step 1: Push Code to GitHub

Make sure your code is in a GitHub repository:

```bash
cd /home/tuananh/Documents/tool-check-tkb

# If not yet a git repo:
git init
git remote add origin https://github.com/YOUR_USERNAME/tool-check-tkb.git

# Commit and push
git add .
git commit -m "Initial webhook + deployment setup"
git push -u origin main
```

---

## Step 2: Create Railway Account

1. Go to **[railway.app](https://railway.app)**
2. Click **"Start a New Project"**
3. Sign up with GitHub (easiest) or email
4. Authorize Railway to access your GitHub repos

---

## Step 3: Create Railway Project

1. In Railway dashboard, click **New Project**
2. Select **"Deploy from GitHub repo"**
3. Search for and select your `tool-check-tkb` repository
4. Click **"Create"**

Railway will automatically detect Python and start building from your repo.

---

## Step 4: Add Environment Variables

Railway will find your `.env` file but **ignore it for security**. Add secrets manually:

1. In Railway project dashboard, go to **Variables** tab
2. Add each secret by clicking **"Add Variable"**:

```
STUDENT_ID                    = your_student_id
PASSWORD                      = your_password
SUPABASE_URL                  = https://xxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY     = eyJhbG... (long string)
SUPABASE_KEY                  = eyJhbGc... (long string)
TELEGRAM_BOT_TOKEN            = 123456:ABCdef... (from @BotFather)
TELEGRAM_CHAT_ID              = 987654321 (your chat ID)
GEMINI_API_KEY                = AI... (optional)
TARGET_SEMESTER               = HK2/2025-2026 (optional)
TELEGRAM_WEBHOOK_SECRET       = your_random_secret_token (generate random)
```

**⚠️ Important**: After adding vars, click **"Save"** and restart the deployment.

---

## Step 5: Set Up Telegram Webhook

Once Railway deployment is live, get the public URL and register with Telegram:

1. Find your Railway app URL:
   - Go to **Railway project → Deployments tab**
   - Click the latest deployment → **View logs**
   - Look for `Uvicorn running on 0.0.0.0:8000`
   - Your public URL is printed in the Railway dashboard (look for domain link)
   - Should look like: `https://tool-check-tkb-production.up.railway.app`

2. Set `TELEGRAM_WEBHOOK_URL` variable:
   ```
   TELEGRAM_WEBHOOK_URL = https://tool-check-tkb-production.up.railway.app/telegram/webhook
   ```
   (Replace `tool-check-tkb-production` with your actual Railway domain)

3. Save and redeploy. The webhook will auto-register with Telegram on startup.

---

## Step 6: Test Deployment

1. Send a test message to your Telegram bot:
   ```
   /start
   ```
   
2. You should get the help message back within 1 second (webhook is fast!)

3. Try creating an appointment:
   ```
   hop nhom-15/04 14:00-B402
   ```

4. Check Railway logs (Deployments tab) for any errors.

---

## Step 7: Enable GitHub Auto-Deploy (Optional)

1. In Railway project, go to **Settings**
2. Find **GitHub Integration** → **Auto Deploy**
3. Enable automatic deploy on push to `main` branch

Now every `git push` will auto-deploy!

---

## Troubleshooting Railway

### Deployment fails: "Playwright install failed"
→ This is expected on first build. Railway will retry. If it persists, try increasing build timeout in project settings.

### Bot doesn't respond
→ Check Railway deployment logs:
   1. Go to **Deployments → Latest → Logs**
   2. Look for errors like "Missing TELEGRAM_BOT_TOKEN"
   3. Verify env vars are saved (UI sometimes doesn't reflect immediately)

### Webhook not registering
→ Check logs for: `"Telegram webhook registered"`
→ If missing, restart deployment: **Deployments → Restart latest**

### High CPU usage
→ Normal when idle. Railway bills by compute time only.

---

## Monthly Cost Estimate

- **Free tier**: $5/month credits
- **Bot webhook**: ~0.1 CPU cores used when idle
- **Monthly bill**: Usually `$0` (within free tier)

---

## Scheduled Jobs (Hourly Data Sync + Daily Morning Notification)

Your bot now supports two scheduled workflows:

### `run_hour.py` - Hourly Data Collection (Steps 1-4)
- **What it does**: Crawls TDTU portal, syncs to Supabase, exports CSV, syncs Google Calendar
- **Frequency**: Every hour
- **Why**: Keeps schedule data fresh across multiple crawl cycles

### `main.py` - Daily Morning Notification (Step 5)
- **What it does**: Sends morning Telegram briefing with today's schedule
- **Frequency**: Once daily at midnight (0:00 AM)
- **Why**: Single consolidated morning notification with all classes & appointments

### Setup Option 1: Railway Cron Jobs (Recommended)

Railway supports scheduled jobs via cron configuration:

1. **Add to `railway.json`** (in `deploy` section):
```json
"scheduledJobs": [
  {
    "command": "python run_hour.py",
    "schedule": "0 * * * *"
  },
  {
    "command": "python main.py",
    "schedule": "0 0 * * *"
  }
]
```

2. **Deploy**:
```bash
git add railway.json
git commit -m "Add scheduled jobs for hourly sync and daily notifications"
git push
```

3. Railway will automatically create and manage these cron jobs.

### Setup Option 2: GitHub Actions (Free Alternative)

If Railway doesn't support scheduled jobs yet:

1. **Create `.github/workflows/schedule_jobs.yml`**:
```yaml
name: Scheduled Data Sync & Notifications

on:
  schedule:
    - cron: '0 * * * *'        # Hourly at :00
    - cron: '0 0 * * *'        # Daily at 00:00 UTC

jobs:
  hourly-sync:
    if: github.event.schedule == '0 * * * *'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python run_hour.py
        env:
          STUDENT_ID: ${{ secrets.STUDENT_ID }}
          PASSWORD: ${{ secrets.PASSWORD }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_ROLE_KEY: ${{ secrets.SUPABASE_SERVICE_ROLE_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}

  daily-notification:
    if: github.event.schedule == '0 0 * * *'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python main.py
        env:
          STUDENT_ID: ${{ secrets.STUDENT_ID }}
          SUPABASE_URL: ${{ secrets.SUPABASE_URL }}
          SUPABASE_KEY: ${{ secrets.SUPABASE_KEY }}
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

2. **Add secrets to GitHub repo** (Settings → Secrets):
   - `STUDENT_ID`, `PASSWORD`, `SUPABASE_URL`, etc. (same as Railway vars)

3. **Optional**: Use `workflow_dispatch` to manually trigger jobs for testing:
```yaml
on:
  schedule:
    - cron: '0 * * * *'
  workflow_dispatch:     # Allows manual trigger from Actions tab
```

### Setup Option 3: Local Cron (If Self-Hosting)

If running on your machine or VPS:

```bash
crontab -e
```

Add:
```
0 * * * * cd /home/tuananh/Documents/tool-check-tkb && python run_hour.py 2>&1 >> logs/run_hour.log
0 0 * * * cd /home/tuananh/Documents/tool-check-tkb && python main.py 2>&1 >> logs/main.log
```

---

## Next Steps

- **Push code updates**: `git push` → auto-deploys
- **Monitor**: Railway dashboard or webhook logs
- **Schedule jobs**: Choose one of the 3 setup options above
- **Test manually**: `python run_hour.py` and `python main.py` locally to verify before deploying

## Alternative: Self-Hosted on Your Machine

If you prefer running on your own machine (less reliable, requires machine always-on), see [SYSTEMD_AUTORUN.md](SYSTEMD_AUTORUN.md).
