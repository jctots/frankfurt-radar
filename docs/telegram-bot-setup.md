# 🤖 Telegram Bot Setup

How to create and configure the Frankfurt Radar Telegram bot for a new deployment.

## 1️⃣ Create the bot via BotFather

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts:
   - **Name:** `Frankfurt Radar` (or your preferred display name)
   - **Username:** must end in `Bot` (e.g. `frankfurt_radar_bot`)
3. BotFather returns a **bot token** — save it securely.
4. Set the bot commands:

```
/setcommands
```

Select your bot, then paste:

```
start - Set up personalized alerts
settings - Edit your alert preferences
mystatus - View your current settings
help - Usage guide and commands
stop - Pause notifications
deletedata - Delete all your data (GDPR)
```

5. Optional — set a description and profile photo via `/setdescription` and `/setuserpic`.

## 2️⃣ Environment variables

Add these to your `.env` file (or however secrets are managed in your deployment):

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Bot token from BotFather (format: `123456:ABC-DEF...`) |
| `TELEGRAM_WEBHOOK_SECRET` | Recommended | Arbitrary string used to validate incoming webhook requests via `X-Telegram-Bot-Api-Secret-Token` header. Generate with `openssl rand -hex 32`. |

These are consumed by the `notifier` container (see `docker-compose.yml`).

## 3️⃣ Webhook configuration

The notifier container runs an HTTP server on port 8443 (configurable via `WEBHOOK_PORT` env var).

### 🔗 Register the webhook with Telegram

After deploying, register the webhook URL with Telegram:

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://your-domain.com/bot/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

Replace `your-domain.com` with your actual domain. The path must be `/bot/webhook`.

### 🔄 Reverse proxy

The webhook endpoint on the notifier container listens on port 8443. Your reverse proxy (e.g. Caddy, nginx) must forward requests from `https://your-domain.com/bot/webhook` to `notifier:8443`.

Example Caddy snippet:

```
your-domain.com {
    handle /bot/webhook {
        reverse_proxy notifier:8443
    }
    handle {
        reverse_proxy web:8080
    }
}
```

### ✅ Verify the webhook

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

Expected response: `"url"` matches your webhook URL, `"has_custom_certificate": false`, `"pending_update_count": 0`.

## 4️⃣ Admin commands

Admin commands (`/status`, `/alerts`, `/poll`) are gated by chat ID. The admin chat ID is read from `config.yaml`:

```yaml
admin_health_notifier:
  telegram_chat_id: 123456789  # your personal Telegram chat ID
```

To find your chat ID, message the bot and check the logs, or use [@userinfobot](https://t.me/userinfobot).

## 5️⃣ Docker Compose

The notifier service in `docker-compose.yml` is preconfigured:

```yaml
notifier:
  environment:
    - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
    - TELEGRAM_WEBHOOK_SECRET=${TELEGRAM_WEBHOOK_SECRET:-}
    - WEBHOOK_PORT=8443
  ports:
    - "8443:8443"
```

No additional configuration is needed beyond setting the env vars.

## 6️⃣ Testing

### 🤖 Verify the bot responds

1. Open Telegram and message your bot.
2. Send `/start` — you should see the welcome message with a "Set up my alerts" button.
3. Complete the onboarding flow to verify preference storage.
4. Send `/mystatus` to confirm preferences were saved.

### 📬 Verify alert delivery

1. Send `/poll` from the admin chat to trigger a poll cycle.
2. Check the notifier container logs for dispatch messages.
3. If you subscribed with default preferences (all sources enabled), you should receive a DM for any new alerts.

### 🌙 Verify quiet hours

1. Use `/settings` to enable quiet hours with a range that covers the current time.
2. Trigger a poll — alerts should be buffered, not delivered.
3. Wait for the quiet hours end time (or temporarily adjust it) — buffered alerts should arrive as a morning briefing.

### 🗑️ Verify GDPR deletion

1. Send `/deletedata` to the bot.
2. Confirm deletion when prompted.
3. Send `/mystatus` — bot should respond that no subscription exists.
