# 🏠 Self-Hosting Guide

Frankfurt Radar is designed to be self-hosted via Docker Compose. This guide covers setup, configuration, and Telegram bot deployment.

## 📋 Prerequisites

- Docker and Docker Compose
- An RMV API key ([register at opendata.rmv.de](https://opendata.rmv.de/))
- A Telegram bot token (if using Telegram notifications) — see [Telegram bot setup](#telegram-bot-setup) below
- A Google Cloud Translation API key (if using Google Translate) — LibreTranslate works without one

## 🚀 Quick start

```bash
git clone https://github.com/jctots/frankfurt-radar
cd frankfurt-radar
cp .env.example .env   # fill in your API keys
docker compose up -d
```

On first start, `config.yaml` is seeded to the `data/` volume. Edit it at `data/config.yaml` — no container rebuild needed for most changes.

## 🐳 Docker services

| Service | Role | Port | Resources |
|---------|------|------|-----------|
| **poller** | Fetches alerts on a cron schedule, translates, writes to DB | — | 0.5 CPU, 256 MB |
| **notifier** | Telegram bot webhook endpoint, subscriber dispatch | 8443 | 0.25 CPU, 128 MB |
| **web** | Flask app serving the status page and API | 8080 | 0.5 CPU, 256 MB |

Optional services (enabled via Docker Compose profiles):

| Service | Profile | Role |
|---------|---------|------|
| **ntfy** | `ntfy` | Push notifications via [ntfy.sh](https://ntfy.sh) |
| **umami** + **umami-db** | `analytics` | Self-hosted, cookie-free usage analytics |
| **mcp** | `mcp` | MCP server for AI assistant integration (Claude Code, etc.) |

Enable profiles:

```bash
docker compose --profile analytics --profile ntfy up -d
```

## 🔑 Environment variables

Set these in your `.env` file. Only secrets belong here — all other configuration goes in `config.yaml`.

| Variable | Required | Description |
|----------|----------|-------------|
| `RMV_API_KEY` | Yes | RMV Open Data API key |
| `TELEGRAM_BOT_TOKEN` | If using Telegram | Bot token from @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | Recommended | Validates incoming webhook requests (generate with `openssl rand -hex 32`) |
| `GOOGLE_TRANSLATE_API_KEY` | If `translator.backend: google` | Google Cloud Translation API key |
| `LIBRETRANSLATE_API_KEY` | No | Auth key for an external LibreTranslate instance |
| `TICKETMASTER_API_KEY` | No | Ticketmaster Discovery API key (for Deutsche Bank Park events) |
| `STADIA_API_KEY` | No | Stadia Maps API key (dark mode map tiles) |
| `RADAR_TAG` | No | Container image tag for version pinning |
| `MCP_PORT` | No | Host port for MCP server (default: 8811) |
| `MCP_ADMIN_KEY` | No | Admin API key for MCP server (unlimited, no rate limiting) |
| `MCP_API_KEYS` | No | Comma-separated API keys for MCP consumers (rate-limited) |

Analytics profile variables (only needed with `--profile analytics`):

| Variable | Description |
|----------|-------------|
| `UMAMI_DB_PASSWORD` | PostgreSQL password for Umami |
| `UMAMI_APP_SECRET` | Umami application secret |
| `UMAMI_USERNAME` | Umami admin username |
| `UMAMI_PASSWORD` | Umami admin password |

## ⚙️ Configuration reference

`data/config.yaml` is the single non-secret configuration source. Changes to poll schedule or quiet hours require a container restart; all other keys are read fresh on each poll cycle.

### 🔄 Polling

```yaml
polling:
  interval_minutes: 10       # Poll frequency (2–60 min)
```

### 📊 Data sources

Each source can be independently enabled/disabled.

```yaml
transport:
  enabled: true
  services:                   # Optional — filter by service type and line
    sbahn: [S1, S2, S3]      # Omit or leave empty for all lines
    ubahn: []                 # Empty = all U-Bahn lines
    tram: []
    bus: []
    regional: []

weather:
  enabled: true
  min_severity: 1             # 1=minor, 2=moderate, 3=severe, 4=extreme

police:
  enabled: true
  max_age_hours: 48           # Drop articles older than this (0=no limit)

autobahn:
  enabled: true
  roads: [A3, A5, A45, A60, A66, A67, A480, A648, A661]
  kinds: [closure, warning]   # warning = real-time incidents (can be noisy)

baustellen:
  enabled: true
  closures: [full]            # full = sperrung:1, partial = sperrung:0

events:
  enabled: true
  advance_days: 7             # Show events this many days before start

sports:
  enabled: true
  advance_days: 7
```

### 📍 Location

```yaml
location:
  radius_km: 50              # Search radius for location-aware pollers
```

### 🌐 Translation

```yaml
translator:
  backend: libretranslate     # "libretranslate" or "google"
  libretranslate_url: http://libretranslate:5000
```

### 📬 Notifications

```yaml
notifier:
  backend: telegram                    # "telegram" or "ntfy"
  telegram_channel: "@YourChannel"     # Channel username or numeric ID
  ntfy_url: http://ntfy               # ntfy server URL (if using ntfy)
  ntfy_topic: frankfurt-radar          # ntfy topic name
  notify_burst_threshold: 10           # Cold-start guard — skip if ≥ N new alerts on first run
  notify_throttle_every: 10            # Pause 3s after every N notifications (0=disabled)
  disabled_sources: []                 # Sources to exclude from notifications
```

### 🖥️ Alert display

```yaml
stale_after_days: 30          # Move alerts to "Long-running" accordion on the status page
cleared_retention_days: 7     # Keep cleared alerts visible for this many days
```

### 🏥 Admin health notifications

```yaml
admin_health_notifier:
  backend: telegram            # "telegram" or "ntfy"
  telegram_chat_id: 123456789 # Your personal Telegram chat ID
  poll_stale_minutes: 20       # Alert if no poll has run in this many minutes (0=disabled)
  ram_warn_pct: 85             # Alert if RAM usage exceeds this percentage
```

### 🌍 Web

```yaml
web:
  allow_manual_poll: false
  site_url: https://your-domain.com
  telegram_channel_url: https://t.me/YourChannel
  telegram_bot_url: https://t.me/YourBot
  github_url: https://github.com/you/frankfurt-radar
  kofi_url: https://ko-fi.com/you           # Optional — donation link in footer
  sponsor_url: https://...                    # Optional — sponsor link in footer
  umami_website_id: xxxxxxxx                  # Umami tracking ID (if analytics profile enabled)
  disabled_default_sources: []                # Sources hidden by default on page load
  impressum_address: "..."                    # Legal operator address (shown on /legal)
  operator_name: "..."
  operator_contact: "..."
```

### 🎉 Static events

Festivals and sports fixtures can be defined in YAML files:

- `data/city_events.yaml` — local events with dates, location (lat/lon), images, and details
- `data/sports_events.yaml` — static sports events (supplements OpenLigaDB and Ticketmaster)

## 🤖 Telegram bot setup

### 1️⃣ Create the bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts — the username must end in `Bot`
3. Save the bot token to your `.env` as `TELEGRAM_BOT_TOKEN`
4. Set bot commands with `/setcommands`:

```
start - Set up personalized alerts
settings - Edit your alert preferences
mystatus - View your current settings
help - Usage guide and commands
stop - Pause notifications
deletedata - Delete all your data (GDPR)
```

### 2️⃣ Configure the webhook

After deploying, register the webhook URL with Telegram:

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://your-domain.com/bot/webhook" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

Your reverse proxy must forward `/bot/webhook` to the notifier container on port 8443.

Caddy example:

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

Verify with:

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"
```

### 3️⃣ Set the admin chat ID

Admin commands (`/status`, `/alerts`, `/visits`, `/poll`, `/ban`, `/unban`) are gated by chat ID:

```yaml
admin_health_notifier:
  telegram_chat_id: 123456789
```

Find your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

See [docs/telegram-bot-setup.md](telegram-bot-setup.md) for the full deployment guide.

## 🤖 MCP server (AI integration)

The MCP server exposes Frankfurt Radar alerts to AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io/). It provides read-only access to active alerts, search, and system status.

**Add to the full stack:**

```bash
docker compose --profile mcp up -d
```

**AI-only deployment** (poller + MCP server, no web/Telegram/ntfy):

```bash
docker compose up -d poller mcp
```

**MCP-only** (if you already have a `radar.db` from another source):

```bash
docker compose up -d mcp
```

### Available tools

| Tool | Description |
|------|-------------|
| `get_active_alerts` | List active alerts, optionally filtered by source |
| `search_alerts` | Keyword search across alert fields |
| `get_alert_details` | Full details for a single alert by ID |
| `get_system_status` | Last poll time, source health, alert counts |
| `get_alert_stats` | Summary statistics by source and severity |

### Authentication

The MCP server supports optional API key authentication. When no keys are configured, it allows unauthenticated access (suitable for local/homelab use).

| Key type | Env var | Rate limited | Purpose |
|----------|---------|--------------|---------|
| Admin | `MCP_ADMIN_KEY` | No | Operator's own use — unlimited access |
| Distributed | `MCP_API_KEYS` | Yes (60 req/60s per key) | External consumers |

Generate keys with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add them to your `.env`:

```bash
MCP_ADMIN_KEY=your_admin_key_here
MCP_API_KEYS=consumer_key_1,consumer_key_2
```

MCP clients authenticate via the `Authorization` header:

```json
{
  "mcpServers": {
    "frankfurt-radar": {
      "type": "sse",
      "url": "http://<host>:8811/sse",
      "headers": {
        "Authorization": "Bearer <your-api-key>"
      }
    }
  }
}
```

When a distributed key exceeds the rate limit, the server returns HTTP 429 with a `Retry-After` header. If `admin_health_notifier.telegram_chat_id` is configured in `config.yaml`, the operator receives a Telegram notification (with 5-minute cooldown per key).

For unauthenticated access (local/homelab), omit both `MCP_ADMIN_KEY` and `MCP_API_KEYS` — the server will accept all requests without requiring a Bearer token:

```json
{
  "mcpServers": {
    "frankfurt-radar": {
      "type": "sse",
      "url": "http://<host>:8811/sse"
    }
  }
}
```

**Timestamps:** The server returns all timestamps in UTC. MCP clients should convert to `Europe/Berlin` (CET/CEST) for display. The web and notifier containers handle their own UTC-to-Frankfurt conversion independently.

## ➕ Adding a new alert source

1. Subclass `BasePoller` in `pollers.py`
2. Implement `fetch() -> list[Alert]` — return normalized `Alert` objects
3. Register it in the source list in `main.py`
4. Add a config section and document the data source license

The rest of the pipeline (translation, caching, deduplication, notification) is source-agnostic.
