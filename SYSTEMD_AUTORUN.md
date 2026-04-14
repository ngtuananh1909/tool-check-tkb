# Auto-Run on Your Own Machine (systemd)

If you prefer running the webhook server on your own Linux machine for 24/7 operation, use `systemd` service.

**Note**: This requires your machine to be always-on. If it shuts down, the bot stops.

---

## Prerequisites

- Linux machine (Ubuntu/Debian recommended)
- Project installed in `/opt/tool-check-tkb` (or equivalent)
- `.env` file with all credentials
- Virtual environment at `.venv/bin/python`

---

## Step 1: Copy Project to /opt

```bash
sudo cp -r /home/tuananh/Documents/tool-check-tkb /opt/
sudo chown -R $USER:$USER /opt/tool-check-tkb
cd /opt/tool-check-tkb
```

---

## Step 2: Verify Virtual Environment

```bash
# Create venv in /opt copy
python3.11 -m venv /opt/tool-check-tkb/.venv
source /opt/tool-check-tkb/.venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

---

## Step 3: Create systemd Service (Root)

Copy the template service file and enable it:

```bash
# Copy systemd service file to system
sudo cp /opt/tool-check-tkb/deploy/telegram-webhook.service /etc/systemd/system/

# Reload systemd daemon
sudo systemctl daemon-reload

# Enable auto-start on boot
sudo systemctl enable telegram-webhook.service

# Start the service now
sudo systemctl start telegram-webhook.service

# Check status
sudo systemctl status telegram-webhook.service
```

---

## Step 4: Verify It's Running

```bash
# Check service status
sudo systemctl status telegram-webhook.service

# View live logs
sudo journalctl -u telegram-webhook.service -f

# Check if port 8000 is listening
sudo netstat -tlnp | grep 8000
# or
sudo ss -tlnp | grep 8000
```

---

## Step 5: Firewall & Network

If you want Telegram to reach the webhook, set up **ngrok** (temporary testing) or **public domain** (production):

### Option A: ngrok (Quick Testing)

```bash
# Install ngrok: https://ngrok.com/
# Run in another terminal:
ngrok http 8000

# Copy forwarding URL, e.g., https://abc123.ngrok.io
# Set env var:
TELEGRAM_WEBHOOK_URL=https://abc123.ngrok.io/telegram/webhook
```

### Option B: Public Domain (Production)

Set up a domain pointing to your machine's public IP, then:

```bash
# Edit .env
TELEGRAM_WEBHOOK_URL=https://yourdomain.example.com/telegram/webhook

# Set up reverse proxy (nginx):
sudo apt-get install nginx certbot python3-certbot-nginx

# Configure nginx to proxy to localhost:8000
# (Ask if you need help with this)

# Get free SSL certificate:
sudo certbot certonly --nginx -d yourdomain.example.com
```

---

## Step 6: Configure TELEGRAM_WEBHOOK_URL

Edit `/opt/tool-check-tkb/.env`:

```bash
sudo nano /opt/tool-check-tkb/.env

# Add or update:
TELEGRAM_WEBHOOK_URL=https://your-public-url/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=your_random_secret_token
```

Then restart:

```bash
sudo systemctl restart telegram-webhook.service
```

---

## Useful Commands

```bash
# Start service
sudo systemctl start telegram-webhook.service

# Stop service
sudo systemctl stop telegram-webhook.service

# Restart (apply new .env changes)
sudo systemctl restart telegram-webhook.service

# View last 50 lines of logs
sudo journalctl -u telegram-webhook.service -n 50

# Follow logs in real-time
sudo journalctl -u telegram-webhook.service -f

# Check if running on port 8000
curl http://localhost:8000/health

# Disable auto-start (but keep manual start capability)
sudo systemctl disable telegram-webhook.service

# Remove service completely
sudo systemctl disable telegram-webhook.service
sudo rm /etc/systemd/system/telegram-webhook.service
sudo systemctl daemon-reload
```

---

## Troubleshooting

### Service fails to start
```bash
sudo journalctl -u telegram-webhook.service -n 100
# Look for error messages
```

### "Permission denied" errors
```bash
# Ensure .venv and .env are readable by your user
sudo chown -R $USER:$USER /opt/tool-check-tkb
chmod 644 /opt/tool-check-tkb/.env
```

### "Port 8000 already in use"
```bash
# Kill process using port 8000
sudo lsof -i :8000
sudo kill -9 <PID>
```

### Webhook not registering with Telegram
→ Check logs for `"Telegram webhook registered"`
→ Verify `TELEGRAM_WEBHOOK_URL` is reachable:
```bash
curl -I https://your-url/telegram/webhook
# Should return 401 or 200 (not connection refused)
```

---

## Security Notes

⚠️ **Keep .env secret!** It contains passwords.

```bash
# Ensure .env is not world-readable
chmod 600 /opt/tool-check-tkb/.env

# Don't commit .env to git
echo ".env" >> /opt/tool-check-tkb/.gitignore
```

---

## Comparison: Railway vs Systemd

| Feature | Railway | Systemd |
|---------|---------|---------|
| **Cost** | Free ($5/mo credits) | Free |
| **Always-on** | Yes | Only if machine on |
| **Auto-deploy** | Yes (git push) | Manual |
| **Setup time** | 10 min | 15 min |
| **Machine needed** | No | Yes + 24/7 power |
| **Monitoring** | Railway dashboard | Manual logs |
| **Recommended** | ✅ Yes | For experienced Linux users |

**Recommendation**: Use **Railway**. It's simpler and more reliable.

---

## Next Steps

- **For Railway**: See [DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)
- **For local testing**: See [LOCAL_SETUP.md](LOCAL_SETUP.md)
