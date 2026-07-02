# 🏠 Self-Hosting Guide

Frankfurt Radar is designed to be self-hosted via Docker Compose. This guide covers setup, configuration, the admin dashboard, the Telegram bot, and the MCP server.

## 📋 Prerequisites

- Docker and Docker Compose
- An RMV API key ([register at opendata.rmv.de](https://opendata.rmv.de/))
- A Telegram bot token (if using Telegram notifications) — see [Telegram bot setup](#-telegram-bot-setup) below
- A Google Cloud Translation API key (if using Google Translate) — LibreTranslate works without one
- A Google Gemini API key (if enabling the strike poller, police geocoding, or City Pulse)

## 🚀 Quick start

```bash
git clone https://github.com/jctots/frankfurt-radar
cd frankfurt-radar
cp .env.example .env   # fill in your API keys
docker compose up -d
```

On first start, `config.yaml`, the event YAML files, and the LLM prompt templates are seeded to the `data/` volume. Edit them there — no container rebuild needed for most changes.

## 🐳 Docker services

| Service | Role | Port | Resources |
|---------|------|------|-----------|
| **poller** | Fetches alerts + radar frames on a cron schedule, runs City Pulse, writes to DB | 8888 (internal trigger API) | 0.5 CPU, 256 MB |
| **notifier** | Telegram bot webhook + alert/pulse dispatch to channel and subscribers | 8443 (internal only) | 0.25 CPU, 128 MB |
| **web** | Flask app serving the status page, API, and admin dashboard | 8080 | 0.5 CPU, 256 MB |
| **mcp** | MCP server for AI assistant integration | 8811 | 0.25 CPU, 128 MB |

The notifier and trigger ports are reachable only on the internal Docker network — your reverse proxy forwards `/bot/webhook` to `notifier:8443`.

Optional services (enabled via Docker Compose profiles):

| Service | Profile | Role |
|---------|---------|------|
| **ntfy** | `staging` | Push notifications via [ntfy.sh](https://ntfy.sh) |
| **umami** + **umami-db** | `production` | Self-hosted, cookie-free usage analytics |
| **caddy** | `production` | Reverse proxy with automatic HTTPS (Let's Encrypt) |

Enable profiles:

```bash
# Production (includes Caddy reverse proxy and Umami analytics)
docker compose --profile production up -d

# Staging (includes ntfy for push notifications)
docker compose --profile staging up -d
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
| `GEMINI_API_KEY` | If strike poller, police geocoding, or City Pulse enabled | Google Gemini API key (Gemini Flash) |
| `TICKETMASTER_API_KEY` | No | Ticketmaster Discovery API key (for Deutsche Bank Park events) |
| `STADIA_API_KEY` | No | Stadia Maps API key (dark mode map tiles) |
| `ADMIN_TOKEN` | For admin dashboard | Password for the web admin dashboard at `/admin` |
| `SUBSCRIBER_CAP` | No | Max bot subscribers (default: 25) |
| `RADAR_TAG` | No | Container image tag for version pinning |
| `MCP_ADMIN_KEY` | No | Admin API key for MCP server (unlimited, no rate limiting) |
| `MCP_API_KEYS` | No | Comma-separated API keys for MCP consumers (rate-limited) |
| `BIND_ADDR` | No | Bind address for host port mappings — set to `127.0.0.1` when using Caddy (default: `0.0.0.0`) |

Production profile variables (only needed with `--profile production`):

| Variable | Description |
|----------|-------------|
| `UMAMI_DB_PASSWORD` | PostgreSQL password for Umami |
| `UMAMI_APP_SECRET` | Umami application secret |
| `UMAMI_USERNAME` | Umami admin username |
| `UMAMI_PASSWORD` | Umami admin password |

## ⚙️ Configuration reference

`data/config.yaml` is the single non-secret configuration source. Changing the poll interval requires a container restart (the crontab is generated at startup); all other keys are read fresh on each poll cycle.

### 🔄 Polling

```yaml
polling:
  interval_minutes: 10       # Poll frequency (1–60 min) — also drives radar frame updates
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

feuerwehr:
  enabled: true
  ttl_hours: 4                # Keep fire alerts active this long after first post

autobahn:
  enabled: true
  roads: [A3, A5, A45, A60, A66, A67, A480, A648, A661]
  kinds: [closure]            # Add "warning" for real-time incidents (noisy in rush hour)

baustellen:
  enabled: true
  closures: [full]            # full = complete closure; add "partial" for lane restrictions

strike:
  enabled: true
  max_age_days: 14            # Drop alerts older than this if valid_until is missing

events:
  enabled: true
  advance_days: 7             # Show events this many days before start

messe:
  enabled: true
  advance_days: 7             # Show trade fairs this many days before start

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
  max_chars: 1500             # Hard cap per translate call — cost circuit-breaker
```

### 🏙️ City Pulse

```yaml
pulse:
  enabled: false              # Opt-in — hourly AI summary + daily digest (needs GEMINI_API_KEY)
```

### 💶 Cost budget

Metered API usage (Gemini tokens, Google Translate characters) is tracked per day and hour. Budget-threshold alerts fire at 50%, 80%, and 100% of the monthly budget.

```yaml
cost:
  monthly_budget: 5.00        # EUR
  usd_to_eur: 0.92
  gemini:
    input_per_million: 0.15   # USD per 1M input tokens
    output_per_million: 0.60
    thinking_per_million: 3.50
  google_translate:
    chars_per_million: 20.00  # USD per 1M characters
```

### 📬 Notifications

```yaml
notifier:
  backend: telegram                    # "telegram" or "ntfy"
  telegram_channel: "@YourChannel"     # Channel username or numeric ID
  status_url: https://your-domain.com  # "Details" link target in briefings
  ntfy_url: http://ntfy                # ntfy server URL (if using ntfy)
  ntfy_topic: frankfurt-radar          # ntfy topic name
  notify_burst_threshold: 10           # Cold-start guard — skip if ≥ N new alerts at once
  notify_throttle_every: 10            # Pause 3s after every N notifications (0=disabled)
  disabled_sources: []                 # Sources never notified (still polled + shown on site)
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

The shipped `config.yaml` has no `web:` section — add one to `data/config.yaml` to configure the status page:

```yaml
web:
  allow_manual_poll: false
  site_url: https://your-domain.com
  telegram_channel_url: https://t.me/YourChannel
  telegram_bot_url: https://t.me/YourBot
  github_url: https://github.com/you/frankfurt-radar
  kofi_url: https://ko-fi.com/you           # Optional — donation link in footer
  sponsor_url: https://...                    # Optional — sponsor link in footer
  umami_url: https://umami.your-domain.com     # Umami instance URL (if production profile enabled)
  umami_website_id: xxxxxxxx                  # Umami tracking ID (if production profile enabled)
  disabled_default_sources: []                # Sources hidden by default on page load
  impressum_address: "..."                    # Legal operator address (shown on /legal)
  operator_name: "..."
  operator_contact: "..."
```

### 🎉 Static events

Festivals, trade fairs, and sports fixtures can be defined in YAML files (seeded to the data volume on first start):

- `data/city_events.yaml` — local events with dates, location (lat/lon), images, and details
- `data/messe_events.yaml` — trade fairs at Messe Frankfurt
- `data/sports_events.yaml` — static sports events (supplements OpenLigaDB and Ticketmaster)

## 🛠️ Admin dashboard

The web container serves an operator dashboard at `/admin`, protected by a cookie session — log in with the value of the `ADMIN_TOKEN` env var. It provides:

- **Server status** — poller timing, per-source health, RAM/load
- **Cost charts** — daily and hourly API spend vs. budget, per service
- **City Pulse debugging** — all three pulse layers visualized: deterministic scores and breakdowns, the LLM prompt/response, and the final output
- **Status overrides** — record corrections when a computed pulse status is wrong (feeds the calibration loop, see [analysis.md](analysis.md))
- **Weight review** — LLM-assisted suggestions for severity weight adjustments based on recorded overrides
- **Manual triggers** — run a poll or pulse on demand
- **Bot bans** — block/unblock bot users

If `ADMIN_TOKEN` is not set, admin login is unavailable.

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
search - Search active alerts by keyword
pulse - Get the latest City Pulse summary
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

Admin commands (`/status`, `/alerts`, `/visits`, `/costs`, `/poll`, `/ban`, `/unban`) are gated by chat ID:

```yaml
admin_health_notifier:
  telegram_chat_id: 123456789
```

Find your chat ID by messaging [@userinfobot](https://t.me/userinfobot).

See [telegram-bot-setup.md](telegram-bot-setup.md) for the full deployment guide.

## 🤖 MCP server (AI integration)

The MCP server exposes Frankfurt Radar alerts to AI assistants via the [Model Context Protocol](https://modelcontextprotocol.io/). It provides read-only access to active alerts, search, City Pulse, and system status.

The MCP server is included in the default profile — no extra flags needed.

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
| `get_city_pulse` | Latest City Pulse summary, optionally with recent history |
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
