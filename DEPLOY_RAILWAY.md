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

## Next Steps

- **Push code updates**: `git push` → auto-deploys
- **Monitor**: Railway dashboard or webhook logs
- **6 AM daily summary**: Update `.github/workflows/daily_tkb.yml` to include appointments query

## Alternative: Self-Hosted on Your Machine

If you prefer running on your own machine (less reliable, requires machine always-on), see [SYSTEMD_AUTORUN.md](SYSTEMD_AUTORUN.md).
