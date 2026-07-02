# 🏗️ Architecture

## 🔭 Overview

Frankfurt Radar runs as independently deployable containers — a **poller**, a **notifier**, a **web server**, and an **MCP server** — sharing a single SQLite database via a Docker volume. In production, a **Caddy** reverse proxy handles TLS termination and hostname-based routing.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                               Docker Compose                                │
│                                                                             │
│  ┌───────────────────────┐   ┌───────────────────┐   ┌───────────────────┐ │
│  │  poller (cron)        │   │  notifier (http)  │   │  web (gunicorn)   │ │
│  │                       │   │  :8443 internal   │   │  :8080            │ │
│  │  main.py  (poll)      │   │                   │   │                   │ │
│  │  radar.py (radar imgs)│──▶│  POST /dispatch   │   │  GET /            │ │
│  │  pulse.py (hourly)    │   │  POST /bot/webhook│   │  GET /alert/<id>  │ │
│  │  trigger.py :8888     │◀──│  (user + admin    │◀──│  GET /admin  (a)  │ │
│  │  ├── POST  /poll      │   │   bot commands)   │   │  /api/status      │ │
│  │  ├── GET   /config    │   │                   │   │  /api/radar/*     │ │
│  │  └── PATCH /config    │   │                   │   │  /api/admin/* (a) │ │
│  └──────────┬────────────┘   └────────┬──────────┘   └─────────┬─────────┘ │
│             │                         │                        │           │
│             └────────────┐     ┌──────┘     ┌──────────────────┘           │
│                          ▼     ▼            ▼                              │
│                     radar_data (volume)                                    │
│                     ├── radar.db     (SQLite WAL)                          │
│                     ├── config.yaml                                        │
│                     ├── prompts/     (editable LLM prompt templates)       │
│                     └── radar/       (weather radar PNG frames)            │
└─────────────────────────────────────────────────────────────────────────────┘
                                                            (a) = admin-only
```

The containers are coupled through the shared database plus two internal HTTP paths:

1. **Poller → notifier**: after each poll cycle, the poller POSTs to `notifier:8443/dispatch`, which posts new alerts to the Telegram channel and dispatches filtered DMs to subscribers.
2. **Notifier / web → poller**: the poller runs a small admin API (`trigger.py`, port 8888, internal Docker network only) with `POST /poll` (trigger an immediate poll), `GET /config`, and `PATCH /config` (deep-merge config update; regenerates the crontab if the poll interval changes). The bot's `/poll` admin command and the web admin dashboard use it.

The **MCP server** container provides read-only access to the alert database for AI assistants via the Model Context Protocol (SSE transport on port 8811). It imports `db.py` and `models.py` directly — no API layer between it and SQLite.

---

## 📦 Poller container

### 🚀 Startup

On container start, `entrypoint.sh`:

1. Seeds `data/config.yaml`, the three event YAML files, and `data/prompts/` from the bundled defaults if absent
2. Generates `/etc/cron.d/frankfurt-radar` from `config.yaml`:
   - `main.py --mode poll` and `radar.py` every `polling.interval_minutes` (default 10)
   - `pulse.py` hourly (City Pulse) and `pulse.py --daily` at 23:00 (daily summary)
3. Injects runtime env vars into the cron environment
4. Starts the `trigger.py` admin API in the background (port 8888)
5. Runs one immediate poll before handing off to `cron -f`

### ⚙️ Alert pipeline

Each poll invocation of `main.py`:

```
main.py
  ├── load config.yaml + env vars
  ├── init_db()                  — create tables if absent (idempotent)
  ├── instantiate pollers        — config-driven
  ├── fetch all alerts           — returns list[Alert]; per-source health recorded
  ├── age filters                — police max_age_hours, strike max_age_days
  ├── mark stale                 — valid_from older than stale_after_days
  ├── sync_alert_cache()         — translate + write to alert_cache table
  ├── clear_expired_alerts() + expire_processed_alerts()
  ├── process_alerts()           — dedup via processed_alerts; cold-start guard
  ├── set_meta("last_polled_at", ...)
  ├── POST NOTIFIER_DISPATCH_URL — hand off notification to the notifier
  ├── write_cost_debug()         — daily API cost snapshot to data/cost_debug/
  └── admin health metrics       — source health, translator, extraction, RAM, load
```

Deduplication and notification are decoupled: the poller only records which alerts have been seen (`processed_alerts`); actual delivery happens in the notifier (see below). The cold-start guard applies on both sides — if a fresh deploy produces more new alerts than `notify_burst_threshold`, they are marked seen / the cursor is advanced without notifying anyone.

### 📡 Pollers

All sources subclass `BasePoller`:

```python
class BasePoller(ABC):
    def fetch(self) -> list[Alert]: ...
```

| Poller | Source | Notes |
|--------|--------|-------|
| `RMVPoller` | HAFAS HIM API | Frankfurt region filter + optional service/line filter |
| `DWDPoller` | BrightSky (DWD proxy) | English fields pre-translated; severity threshold from config |
| `PolizeiPoller` | Presseportal RSS | Age window from config; LLM location extraction for map pins |
| `FeuerwehrPoller` | Bluesky AT Protocol | @feuerwehrffm.bsky.social posts; district geocoding; TTL-based expiry |
| `AutobahnPoller` | Autobahn API | Road filter, radius_km, kind filter (warning/closure) |
| `BaustellenPoller` | City of Frankfurt WFS | GeoJSON geometry parsing; sperrung filter |
| `StrikePoller` | ver.di Hessen + hessenschau RSS | Gemini Flash LLM extraction for dates/location; cross-feed dedup |
| `StaticEventsPoller` | `city_events.yaml` | Festivals; `source="events"` (default) |
| `StaticEventsPoller` | `messe_events.yaml` | Trade fairs; `source="messe"` |
| `StaticEventsPoller` | `sports_events.yaml` | Sports fixtures; `source="sports"` |
| `OpenLigaPoller` | OpenLigaDB API | Eintracht Frankfurt home games; runs at most once per day |
| `TicketmasterPoller` | Ticketmaster API | Deutsche Bank Park events; runs at most once per day |

Adding a new source means subclassing `BasePoller` and registering it in `main.py` — the rest of the pipeline is source-agnostic.

### 📄 Alert model

```python
@dataclass
class Alert:
    id: str                    # stable dedup key
    source: str                # "rmv" | "dwd" | "polizei" | "feuerwehr" | "autobahn"
                               # | "baustellen" | "strike" | "events" | "messe" | "sports"
    title: str                 # German, pre-translation
    body: str                  # German, HTML-stripped
    url: Optional[str]
    valid_until: Optional[str] # ISO UTC
    valid_from: Optional[str]  # ISO UTC
    service: Optional[str]     # "S-Bahn" | "U-Bahn" | "Tram" | "Bus" | "Regional" | ...
    lines: list[str]           # affected line codes
    published_at: Optional[str]
    severity: Optional[int]    # 1–4 (DWD only)
    lat, lon: Optional[float]  # map pin
    location_label: Optional[str]
    image: Optional[str]       # image URL (events/sports)
    stale: bool                # older than stale_after_days — "Long-running" accordion
    icon: Optional[str]        # frozen per-alert weather icon (DWD)
```

### 🌐 Translation

Two pluggable backends, selected by `translator.backend` in config:

| Backend | Notes |
|---------|-------|
| `libretranslate` | Self-hosted; no API key for own instance |
| `google` | Cloud Translation API v2; used on public instance |

`translate_alert(alert, config)` returns `(en_title, en_body)`. DWD alerts arrive in English from BrightSky and skip translation.

Two cost controls sit in front of the paid backend:

- **`translator.max_chars`** (default 1500) — hard cap per translate call; a circuit-breaker against unusually long alert bodies, not routine truncation.
- **Translation variant cache** — the `translation_variants` table stores every translated (title, body) pair keyed by alert ID and a content hash. Re-polls of unchanged text are free, and the cache is also consulted **across alert IDs** by content hash + source, so sources that re-issue identical content under new IDs (e.g. DWD re-issuing a warning with a new CAP ID) don't pay for re-translation.

### 🤖 LLM integration (Gemini Flash)

All LLM calls use Google Gemini Flash with a shared `GEMINI_API_KEY` and the same retry pattern (3 attempts, exponential backoff on 429). Each call is driven by an editable prompt template in `prompts/` (YAML frontmatter for model, temperature, and thinking budget + template body), seeded to the data volume on first start.

| Use | Module | Prompt | Notes |
|-----|--------|--------|-------|
| Strike extraction | `extraction.py` | `strike_extraction.md` | Structured dates/location/summary from German press releases |
| Strike cross-feed dedup | `extraction.py` | `strike_dedup.md` | Confirms duplicates after a date-overlap heuristic |
| Police location extraction | `extraction.py` | `police_location.md` | Geocodes police reports for map pins |
| City Pulse synthesis | `pulse.py` | `pulse.md` | Hourly situational summary; `thinking_budget: 1024` |
| Daily summary | `pulse.py` | `daily_summary.md` | Compresses 24 hourly pulses at 23:00 |
| Weight review | `web/app.py` | `weight_review.md` | Admin-triggered severity-weight calibration suggestions |

**City Pulse** (`pulse.py` + `pulse_categories.py`) combines deterministic analysis with LLM synthesis: severity-weighted scoring, status (with hysteresis and absolute floors), and trend are all computed deterministically per category; the LLM writes the narrative and may correct a trend only via a logged, content-based override. It runs as a standalone cron job in the poller container; hourly pulses land in `pulse_history` and daily summaries in `pulse_daily_summary`, with the daily digest fed back as multi-day narrative context into future pulses. Extraction health is tracked via `extraction_ok()`, pulse health via `pulse_ok()`. See [analysis.md](analysis.md) for the full methodology.

### 🌧️ Weather radar poller

`radar.py` runs on the same cron cadence as the poll and downloads precipitation radar frames from the DWD WMS (`Radar_wn-product_1x1km_ger` layer) into `data/radar/` — a rolling window of 18 observation frames plus 12 forecast frames, replaced each run. The web container serves them via `/api/radar/*` for the animated radar overlay.

### 💶 Cost tracking

Every metered API call (Gemini tokens, Google Translate characters) is recorded in `api_usage` (daily) and `api_usage_hourly`. `write_cost_debug()` appends a daily JSONL snapshot to `data/cost_debug/`. Costs surface in three places: the bot's `/costs` admin command, the admin dashboard cost charts, and budget-threshold alerts (50/80/100% of `cost.monthly_budget`).

---

## 🤖 Notifier container

The notifier handles Telegram bot interactions and all alert delivery. Port 8443 is internal to the Docker network — in production, Caddy forwards `/bot/webhook` to it; the port is not published on the host.

### 🔗 Endpoints

| Endpoint | Caller | Purpose |
|----------|--------|---------|
| `POST /bot/webhook` | Telegram | Bot updates; validated via `X-Telegram-Bot-Api-Secret-Token` |
| `POST /dispatch` | poller | Deliver newly cached alerts (channel + subscriber DMs) |

### 📤 Alert dispatch

`dispatcher.py` runs on each `/dispatch` call:

1. Read alerts cached since the `last_notified_at` cursor
2. Cold-start guard — if the batch exceeds `notify_burst_threshold`, advance the cursor silently
3. Skip sources listed in `notifier.disabled_sources`
4. Post each alert to the public channel (or ntfy topic)
5. Fan out to subscribers (`subscriber_dispatch.py`): match preferences (sources, service, lines, roads, closure type, keywords), check per-subscriber dedup via `sent_alerts`, buffer into `quiet_buffer` during quiet hours, otherwise DM immediately
6. Subscribers who blocked the bot are deactivated automatically

City Pulse DMs are delivered on the same path: when a subscriber's chosen `pulse_time` hour arrives, the latest pulse is sent once per day.

### 👤 User commands

| Command | Action |
|---------|--------|
| `/start` | Subscribe + interactive preference onboarding (capped — see below) |
| `/settings` | Re-enter preference wizard with current settings pre-selected |
| `/mystatus` | Display current preferences and subscription status |
| `/search` | Search active alerts by keyword (interactive paginated results) |
| `/pulse` | Get the latest City Pulse summary on demand |
| `/help` | Command reference and usage guide |
| `/stop` | Set `active=0` — pauses delivery, keeps preferences |
| `/deletedata` | Delete subscriber + sent_alerts + conversation_state records |

New sign-ups are capped (`SUBSCRIBER_CAP` env var, default 25) while the service is in its test phase; the bot replies with a friendly at-capacity message beyond that.

### 🔧 Admin commands

Gated by `chat_id` matching `admin_health_notifier.telegram_chat_id`:

| Command | Action |
|---------|--------|
| `/status` | Health dashboard — poller timing, source health, RAM/load, subscriber count |
| `/alerts` | List current active alerts grouped by source |
| `/visits` | Recent visitor/event statistics (via Umami) |
| `/costs` | Month-to-date API costs vs. budget |
| `/poll` | Trigger a manual poll cycle (via the poller trigger API) |
| `/ban` / `/unban` | Block / unblock a user from the bot |

### 🛡️ Rate limiting

Incoming bot messages are rate-limited to 30 per 60 s per chat, with a 5-minute cooldown after a breach (and an admin notification the first time).

### 🌅 Quiet hours and morning briefing

At the configured quiet hours end time:

1. Query `quiet_buffer` for all buffered alerts per subscriber
2. Group by source, format as morning briefing with missed alerts + upcoming events
3. Send briefing, clear buffer, update `last_briefing_at`
4. If no alerts were buffered, no briefing is sent

---

## 🌐 Web container

Flask app served by gunicorn. The public routes are read-only; admin routes are session-gated.

### 🛣️ Routes

**Public:**

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Status page (single-page app) |
| `/alert/<alert_id>` | GET | Permalink — status page focused on one alert |
| `/api/status` | GET | JSON: `{updated_at, alerts: [...]}` from `alert_cache` |
| `/api/poll` | POST | Triggers a poll; disabled unless `web.allow_manual_poll: true` |
| `/api/radar/frames` | GET | List available radar observation/forecast frames |
| `/api/radar/obs/<file>` | GET | Serve radar observation PNG |
| `/api/radar/forecast/<file>` | GET | Serve radar forecast PNG |
| `/pulse-methodology` | GET | Public "how City Pulse works" page |
| `/api/pulse-methodology-data` | GET | Live data backing the methodology page |
| `/legal` | GET | Impressum page |
| `/robots.txt` | GET | SEO directives with sitemap |

**Admin** (cookie session; login with the `ADMIN_TOKEN` env var):

| Route | Description |
|-------|-------------|
| `/admin/login`, `/admin/logout` | Session management |
| `/admin` | Admin dashboard SPA |
| `/api/admin/data` | Pulse debug data — all three City Pulse layers |
| `/api/admin/cost-history` | Daily cost history for charts |
| `/api/admin/server-status` | Poller/server health snapshot |
| `/api/admin/poll`, `/api/admin/pulse` | Manual poll / pulse triggers |
| `/api/admin/weight-review` | LLM-assisted severity weight review |
| `/api/admin/overrides` | Record/list/delete pulse status overrides |
| `/api/admin/bans`, `/api/admin/ban`, `/api/admin/unban` | Bot user ban management |

### 🖥️ Status page

Single-page app with no build step:

- **Desktop**: left-panel alert feed + right-panel Leaflet map
- **Mobile**: full-height alert list; tap to open full-screen map overlay
- Map tiles: CartoDB Dark Matter (dark mode) / OSM Standard (light mode) — no API key required
- Alert markers: clustered with `leaflet.markercluster`; DWD alerts as floating panel
- Filter bar: source toggles, service/severity dropdowns, lines popup, search, future events toggle
- City Pulse overlay: hourly AI summary + per-category status icons (see [analysis.md](analysis.md))
- Weather overlay: current conditions + daily forecast, fetched client-side from BrightSky
- Weather radar playback: animated observation + forecast frames from `/api/radar/*`
- Rotating ticker: animated headline bar on desktop
- Dark mode and filter state persisted in `localStorage`
- Browser notifications via Web Push API (opt-in)

---

## 🤖 MCP server container (optional)

Read-only MCP server exposing alert data to AI assistants (Claude Code, etc.) via SSE transport. Included in the default profile.

### 🔧 Tools

| Tool | Description |
|------|-------------|
| `get_active_alerts(source?)` | List active alerts, optional source filter |
| `search_alerts(query)` | Token-based AND keyword search |
| `get_alert_details(alert_id)` | Single alert by ID |
| `get_city_pulse(include_history?)` | Latest City Pulse, optionally with recent history |
| `get_system_status()` | Last poll time, source health, counts |
| `get_alert_stats()` | Summary by source and severity |

The server reuses `db.py` query functions and `models.py` formatters. It reads from the shared SQLite database and `config.yaml` (via the shared `radar_data` volume).

### 🔐 Authentication and rate limiting

The auth layer (`mcp/auth.py`) supports two key tiers via ASGI middleware:

| Key type | Env var | Rate limited | Purpose |
|----------|---------|--------------|---------|
| Admin | `MCP_ADMIN_KEY` | No | Operator's own use |
| Distributed | `MCP_API_KEYS` (comma-separated) | Yes (60 req/60s) | External consumers |
| Neither set | — | — | Open access (local/homelab) |

Rate limiting uses an in-memory sliding window per key. When a distributed key exceeds the limit, the server returns HTTP 429 with a `Retry-After` header and sends an admin notification via Telegram (with 5-minute cooldown per key, using `admin_health_notifier.telegram_chat_id` from `config.yaml`).

---

## 🗄️ Database

SQLite at `data/radar.db` with WAL mode. Thirteen tables:

| Table | Purpose | Key fields |
|-------|---------|-----------|
| `processed_alerts` | Deduplication log | `alert_id` (PK), `source`, `valid_until`, `first_seen_at` |
| `alert_cache` | Translated alerts for the status page | `alert_id` (PK), `title_en`, `body_en`, `severity`, `lat`/`lon`, `service`, `lines` (JSON), `image`, `stale`, `removed_at`, `icon` |
| `translation_variants` | Translation cache | `alert_id`, `text_hash`, `source`, `title_en`, `body_en` — reused across polls and across alert IDs |
| `subscribers` | Telegram bot subscribers | `chat_id` (UNIQUE), `preferences` (JSON), `active`, `created_at`, `conversation_state` (JSON), `last_briefing_at` |
| `sent_alerts` | Per-subscriber delivery history | `subscriber_id` (FK), `alert_id`, `sent_at` |
| `quiet_buffer` | Alerts buffered during quiet hours | `subscriber_id` (FK), `alert_id`, `buffered_at` |
| `pulse_history` | Hourly City Pulse outputs | `generated_at`, `title`, `summary`, `categories` (JSON), `recommendation` |
| `pulse_daily_summary` | 23:00 daily digests | `date`, `summary` |
| `category_snapshots` | Hourly deterministic scores per category | `snapshot_at`, `category`, `ongoing_score`, `projected_score`, `upcoming_score` |
| `status_overrides` | Admin corrections to pulse status | `pulse_at`, `category`, `computed_status`, `override_status`, `reason` |
| `api_usage` | Daily metered API usage | `date`, `service`, tokens/characters |
| `api_usage_hourly` | Hourly metered API usage | `date`, `hour`, `service`, tokens/characters |
| `meta` | Key-value store | `last_polled_at`, `last_notified_at`, `last_sports_polled_at`, `admin_health`, `source_health`, … |

`alert_cache` is rebuilt each poll cycle to match currently active alerts. `processed_alerts` is additive and expires entries as alerts go stale.

---

## 🔄 Data flow

```
RMV / DWD / Polizei / Feuerwehr / Autobahn / Baustellen / Strike / Events / Messe / Sports
         │
         ▼
    pollers.py           ← fetch(), returns list[Alert] (German)
         │
         ▼
       db.py             ← sync_alert_cache() — translate (with variant cache) + write
         │               ← clear_expired_alerts(), expire_processed_alerts()
         ▼
    pipeline.py          ← get_unseen_alerts() → cold-start guard → mark_seen
         │
         ▼
    POST notifier:8443/dispatch
         │
    dispatcher.py        ← alerts since last_notified_at cursor
         ├── notifications.py   → Telegram channel post / ntfy push
         └── subscriber_dispatch.py → preference match → DM or quiet_buffer

Status page:
    web/app.py GET /api/status → db.get_status_json() → alert_cache → browser

Bot interaction:
    Telegram → Caddy → notifier:8443 → bot.py → db.py (subscribers, sent_alerts, quiet_buffer)

City Pulse:
    pulse.py (hourly cron) → pulse_categories.py scores → Gemini Flash → pulse_history
```

---

## ⚙️ Configuration

`config.yaml` is the single non-secret configuration source. It lives in the `data/` volume and is editable at runtime. `.env` holds secrets only.

`entrypoint.sh` reads `config.yaml` once at container start to generate the crontab. Changing the poll interval requires a container restart — or a `PATCH /config` through the trigger API, which regenerates the crontab in place. All other keys are read fresh on each invocation.

See [self-hosting.md](self-hosting.md) for the full configuration reference.
