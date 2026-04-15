# Quick Start Guide - Run & Deploy

Pick your option below:

---

## ⚡ Option 1: Run Locally (Testing)

Perfect for development and testing locally on your machine.

**Time**: 5 minutes

```bash
source .venv/bin/activate
uvicorn webhook_app:app --reload
```

Then send a test message to your Telegram bot. See [LOCAL_SETUP.md](LOCAL_SETUP.md) for details.

Tip: set `CRAWLER_WEEKS_AHEAD=2` in `.env` so each run syncs current week + next 2 weeks to Google Calendar.

---

## ☁️ Option 2: Deploy to Railway (Recommended Production)

Your bot runs **24/7** on Railway's free tier ($5/mo credits). When you `git push`, it auto-deploys.

**Time**: 15 minutes

1. Push code to GitHub:
   ```bash
   git add .
   git commit -m "Add webhook + Railway setup"
   git push
   ```

2. Go to [railway.app](https://railway.app)
3. Connect GitHub and create new project
4. Add env vars (copy from your `.env`)
5. Get Railway URL and set `TELEGRAM_WEBHOOK_URL` env var
6. Restart deployment
7. Done! Send `/start` to bot to test

→ Full guide: [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)

**Cost**: $0 (free tier covers this)

---

## 🏠 Option 3: Auto-Run on Your Machine (Linux)

Runs webhook as a systemd service on your Linux machine. Requires machine always-on.

**Time**: 10 minutes | **Cost**: Your electricity

```bash
sudo cp -r . /opt/tool-check-tkb
sudo systemctl enable telegram-webhook.service
sudo systemctl start telegram-webhook.service
```

→ Full guide: [SYSTEMD_AUTORUN.md](SYSTEMD_AUTORUN.md)

---

## 📊 Comparison

| | Local | Railway | Systemd |
|---|---|---|---|
| **Cost** | Free | Free (credits) | Your power bill |
| **Always-on** | ❌ | ✅ | ✅ (if machine on) |
| **Ease** | ⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐ |
| **Recommended** | Testing | **✅ Yes** | Linux experts |

---

## 🚀 Next: Add 6 AM Daily Summary

After deployment is working, add your appointments to the 6 AM daily notification:

1. Update `.github/workflows/daily_tkb.yml` to include appointments:
   ```python
   # In main.py, after fetching schedule, also fetch appointments
   appointments = get_today_appointments(student_id)
   send_combined_schedule_plus_appointments(classes, appointments)
   ```

2. Push to GitHub → auto-runs at 6 AM daily

---

## ✅ Verification Checklist

After choosing your deployment option:

- [ ] Webhook server running (check logs)
- [ ] Send `/start` to Telegram bot → get help message back
- [ ] Send appointment: `hop nhom-15/04 14:00-B402` → confirmation received
- [ ] Send `/today` → list today's appointments
- [ ] Check Supabase dashboard → new appointment appears in `appointments` table
- [ ] Monitor logs for errors (Railway dashboard or `journalctl` for systemd)

---

## ❓ Common Questions

**Q: Which option should I pick?**  
A: **Railway**. Best balance of simplicity + reliability + cost.

**Q: Can I run locally and still chat with bot 24/7?**  
A: No. Only when your laptop is on and script running. Use Railway for always-on.

**Q: How much does Railway cost?**  
A: Free! You get $5/month credits (enough for this app).

**Q: Can I test webhook locally before deploying?**  
A: Yes, use ngrok: `ngrok http 8000` and set `TELEGRAM_WEBHOOK_URL` to the ngrok URL.

---

## 📞 Troubleshooting

### Bot doesn't respond to Telegram messages
1. Check webhook is registered: `curl https://your-railway-url/health`
2. Check Railway logs for errors
3. Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in env vars
4. Restart deployment

### "Missing database field" error
1. Run `supabase/init_tables.sql` to create schema
2. Verify `SUPABASE_SERVICE_ROLE_KEY` has write permissions

### Webhook URL keeps changing
→ Use a custom domain or set `TELEGRAM_DELETE_WEBHOOK_ON_SHUTDOWN=false` to persist

---

## 🎯 Your Next Steps

1. **Pick an option above** (local, Railway, or systemd)
2. **Follow the guide** for your choice
3. **Test appointment creation** via Telegram
4. **Monitor logs** and ensure no errors
5. **Add 6 AM summary** (optional but recommended)

Good luck! 🚀
