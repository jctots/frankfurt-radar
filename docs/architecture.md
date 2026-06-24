# 🏗️ Architecture

## 🔭 Overview

Frankfurt Radar runs as independently deployable containers — a **poller**, a **notifier**, a **web server**, and an **MCP server** — sharing a single SQLite database via a Docker volume. In production, a **Caddy** reverse proxy handles TLS termination and hostname-based routing.

```
┌────────────────────────────────────────────────────────────────────────────┐
│                              Docker Compose                               │
│                                                                            │
│  ┌───────────────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │  poller (cron)        │  │  notifier (http)  │  │  web (gunicorn)    │  │
│  │                       │  │                   │  │                    │  │
│  │  main.py              │  │  bot.py           │  │  GET /             │  │
│  │  ├── pollers.py       │  │  ├── /start       │  │  GET /api/status   │  │
│  │  ├── pipeline.py      │  │  ├── /settings    │  │  GET /api/radar/*  │  │
│  │  ├── extraction.py    │  │  ├── /mystatus    │  │  POST /api/poll    │  │
│  │  ├── translation.py   │  │  ├── /help        │  │  GET /legal        │  │
│  │  ├── notifications.py │  │  ├── /stop        │  │                    │  │
│  │  └── db.py (write)    │  │  ├── /deletedata  │  │                    │  │
│  │                       │  │  ├── /search      │  │                    │  │
│  │                       │  │  ├── /status (a)  │  │                    │  │
│  │                       │  │  ├── /alerts (a)  │  │  db.py (read)      │  │
│  │                       │  │  ├── /visits (a)  │  │                    │  │
│  │                       │  │  ├── /ban    (a)  │  │                    │  │
│  │                       │  │  ├── /unban  (a)  │  │                    │  │
│  │                       │  │  └── /poll   (a)  │  │                    │  │
│  └──────────┬────────────┘  └────────┬─────────┘  └─────────┬──────────┘  │
│             │                        │                      │              │
│             └────────────┐    ┌──────┘    ┌─────────────────┘              │
│                          ▼    ▼           ▼                                │
│                    radar_data (volume)                                     │
│                    ├── radar.db    (SQLite WAL)                            │
│                    └── config.yaml                                         │
└────────────────────────────────────────────────────────────────────────────┘
                                                          (a) = admin-only
```

The containers share no direct communication — the database is the only coupling point.

The **MCP server** container provides read-only access to the alert database for AI assistants via the Model Context Protocol (SSE transport on port 8811). It imports `db.py` and `models.py` directly — no API layer between it and SQLite.

---

## 📦 Poller container

### 🚀 Startup

On container start, `entrypoint.sh`:

1. Seeds `data/config.yaml` from the bundled default if none exists
2. Generates `/etc/cron.d/frankfurt-radar` from `config.yaml` — poll interval, daily hour, and quiet hours are all dynamic
3. Injects runtime env vars into the cron environment
4. Runs one immediate poll before handing off to `cron -f`

### ⚙️ Alert pipeline

Each cron invocation calls `main.py` with `--mode poll` or `--mode daily`:

```
main.py
  ├── load config.yaml + env vars
  ├── init_db()                — create tables if absent (idempotent)
  ├── instantiate pollers      — config-driven
  ├── fetch all alerts         — returns list[Alert]
  ├── sync_alert_cache()       — translate + write to alert_cache table
  ├── expire_processed_alerts()
  ├── process_alerts()         — dedup, notify (poll) or summarize (daily)
  └── set_meta("last_polled_at", ...)
```

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
| `PolizeiPoller` | Presseportal RSS | 24h window; title-only in public mode |
| `AutobahnPoller` | Autobahn API | Road filter, radius_km, kind filter (warning/closure) |
| `BaustellenPoller` | City of Frankfurt WFS | GeoJSON geometry parsing; sperrung filter |
| `StrikePoller` | ver.di Hessen + hessenschau RSS | Gemini Flash LLM extraction for dates/location; cross-feed dedup |
| `StaticEventsPoller` | `city_events.yaml` | Festivals; `source="events"` (default) |
| `StaticEventsPoller` | `messe_events.yaml` | Trade fairs; `source="messe"` |
| `StaticEventsPoller` | `sports_events.yaml` | Sports fixtures; `source="sports"` |
| `OpenLigaPoller` | OpenLigaDB API | Eintracht Frankfurt home games |
| `TicketmasterPoller` | Ticketmaster API | Deutsche Bank Park events |

Adding a new source means subclassing `BasePoller` and registering it in `main.py` — the rest of the pipeline is source-agnostic.

### 📄 Alert model

```python
@dataclass
class Alert:
    id: str                    # stable dedup key
    source: str                # "rmv" | "dwd" | "polizei" | "autobahn" | "baustellen" | "strike" | "events" | "sports"
    title: str                 # German, pre-translation
    body: str                  # German, HTML-stripped
    url: Optional[str]
    valid_until: Optional[str] # ISO UTC
    valid_from: Optional[str]  # ISO UTC
    service: Optional[str]     # "S-Bahn" | "U-Bahn" | "Tram" | "Bus" | "Regional"
    lines: list[str]           # affected line codes
    published_at: Optional[str]
    severity: Optional[int]    # 1–4 (DWD only)
    lat, lon: Optional[float]  # map pin
    location_label: Optional[str]
    image: Optional[str]       # image URL (events/sports)
    icon: Optional[str]        # display icon
```

### 🔀 Pipeline modes

**Poll mode** — runs every N minutes:

1. `get_unseen_alerts()` — checks `processed_alerts` for deduplication
2. Cold-start guard — if `len(new_alerts) >= notify_burst_threshold`, mark all seen silently (prevents notification flood on fresh deploy)
3. For each new alert: translate, notify, mark seen. Throttle pause every N notifications.

**Daily mode** — runs once per day (ntfy backend only):

Collects active alerts by source, groups into sections, sends a single summary notification, marks all seen.

### 🌐 Translation

Two pluggable backends, selected by `translator.backend` in config:

| Backend | Notes |
|---------|-------|
| `libretranslate` | Self-hosted; no API key for own instance |
| `google` | Cloud Translation API v2; used on public instance |

`translate_alert(alert, config)` returns `(en_title, en_body)`. DWD alerts arrive in English from BrightSky and skip translation.

### 🤖 LLM integration (Gemini Flash)

Frankfurt Radar uses Google Gemini Flash for two distinct purposes:

**Strike extraction** (`extraction.py`) — structured data extraction from German press releases. Used by `StrikePoller` to extract strike dates, locations, affected services, and English summaries from RSS feed content. Also powers cross-feed deduplication — when two feeds report the same strike, a date-overlap heuristic followed by an LLM confirmation call suppresses the duplicate. Health is tracked via `extraction_ok()` and shown in the `/status` admin dashboard as "extraction".

**City Pulse synthesis** (`pulse.py` + `pulse_categories.py`) — hourly situational summary combining deterministic analysis with LLM synthesis. Alerts are classified as ongoing or upcoming per category lookahead window, and severity-weighted based on source-specific fields (DWD severity level, RMV service type, closure vs. warning, event impact). Hourly snapshots of per-category scores are stored in `category_snapshots`; time-series are aggregated at each category's natural interval (transport hourly/24h, weather 6-hourly/3d, roadworks daily/4w, etc.) and fed to the LLM. The LLM judges per-category status using domain-specific labels (e.g. transport: clear/delays/disrupted/paralyzed) and trend (improving/stable/worsening), and writes the summary and recommendation. Runs as a standalone cron job in the poller container. Uses editable prompt templates from `prompts/` (YAML frontmatter + template body), seeded to the data volume on first start. Thinking is enabled (`thinkingBudget: 4096`) for spatial reasoning and cross-source correlation. A daily summary job at 23:00 compresses 24 hourly pulses into a one-paragraph digest, which is fed as multi-day narrative context into future hourly pulses. Health is tracked via `pulse_ok()`. See [analysis.md](analysis.md) for the full methodology.

Both modules share the same `GEMINI_API_KEY` environment variable and follow the same retry pattern (3 attempts, exponential backoff on 429).

### 📬 Notifications

Two pluggable backends, selected by `notifier.backend` in config:

| Backend | Targets |
|---------|---------|
| `telegram` | Channel post (unfiltered) + subscriber DMs (filtered by preferences) |
| `ntfy` | Push to configured topic; optional daily summary mode |

---

## 🤖 Notifier container

The notifier handles Telegram bot interactions and subscriber dispatch.

### 🔗 Webhook

Listens on port 8443 for Telegram webhook requests. Validates incoming requests via `X-Telegram-Bot-Api-Secret-Token` header.

### 👤 User commands

| Command | Action |
|---------|--------|
| `/start` | Subscribe + interactive preference onboarding |
| `/settings` | Re-enter preference wizard with current settings pre-selected |
| `/mystatus` | Display current preferences and subscription status |
| `/search` | Search active alerts by keyword (interactive paginated results) |
| `/help` | Command reference and usage guide |
| `/stop` | Set `active=0` — pauses delivery, keeps preferences |
| `/deletedata` | Delete subscriber + sent_alerts + conversation_state records |

### 🔧 Admin commands

Gated by `chat_id` matching `admin_health_notifier.telegram_chat_id`:

| Command | Action |
|---------|--------|
| `/status` | Health dashboard — poller timing, source health, RAM/load, subscriber count |
| `/alerts` | List current active alerts grouped by source |
| `/visits` | Recent visitor/event statistics |
| `/poll` | Trigger a manual poll cycle |
| `/ban` | Block a user from using the bot |
| `/unban` | Unblock a previously banned user |

### 📤 Subscriber dispatch

When the poller writes new alerts, the notifier dispatches them to subscribers:

1. For each new alert, find all active subscribers whose preferences match (source, service, line, severity filters)
2. Check deduplication via `sent_alerts` table
3. If subscriber is in quiet hours: buffer the alert in `quiet_buffer` table
4. Otherwise: send the DM immediately
5. Rate limit: 30 hits per 60s per chat_id; 5-minute cooldown after breach

### 🌅 Quiet hours and morning briefing

At the configured quiet hours end time:

1. Query `quiet_buffer` for all buffered alerts per subscriber
2. Group by source, format as morning briefing with missed alerts + upcoming events
3. Send briefing, clear buffer, update `last_briefing_at`
4. If no alerts were buffered, no briefing is sent

---

## 🌐 Web container

Flask app served by gunicorn. Read-only — no API keys, no write access to the database.

### 🛣️ Routes

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Status page (single-page app) |
| `/api/status` | GET | JSON: `{updated_at, alerts: [...]}` from `alert_cache` |
| `/api/poll` | POST | Triggers poll subprocess; disabled when `web.allow_manual_poll: false` |
| `/api/radar/frames` | GET | List available radar observation/forecast frames |
| `/api/radar/obs/<file>` | GET | Serve radar observation PNG |
| `/api/radar/forecast/<file>` | GET | Serve radar forecast PNG |
| `/legal` | GET | Impressum page |
| `/robots.txt` | GET | SEO directives with sitemap |

### 🖥️ Status page

Single-page app with no build step:

- **Desktop**: left-panel alert feed + right-panel Leaflet map
- **Mobile**: full-height alert list; tap to open full-screen map overlay
- Map tiles: CartoDB Dark Matter (dark mode) / OSM Standard (light mode) — no API key required
- Alert markers: clustered with `leaflet.markercluster`; DWD alerts as floating panel
- Filter bar: source toggles, service/severity dropdowns, lines popup, search, future events toggle
- Pulse indicator: live status dot (green/yellow/red)
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
| `get_system_status()` | Last poll time, source health, counts |
| `get_alert_stats()` | Summary by source and severity |

The server reuses `db.py` query functions (`get_all_active_alerts`, `search_active_alerts`, `get_status_json`) and `models.py` formatters. It reads from the shared SQLite database and `config.yaml` (via the shared `radar_data` volume).

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

SQLite at `data/radar.db` with WAL mode. Six tables:

| Table | Purpose | Key fields |
|-------|---------|-----------|
| `processed_alerts` | Deduplication log | `alert_id` (PK), `source`, `valid_until`, `first_seen_at` |
| `alert_cache` | Translated alerts for the status page | `alert_id` (PK), `title_en`, `body_en`, `severity`, `lat`/`lon`, `service`, `lines` (JSON), `image`, `stale`, `removed_at`, `icon` |
| `subscribers` | Telegram bot subscribers | `chat_id` (UNIQUE), `preferences` (JSON), `active`, `created_at`, `conversation_state` (JSON), `last_briefing_at` |
| `sent_alerts` | Per-subscriber delivery history | `subscriber_id` (FK), `alert_id`, `sent_at` |
| `quiet_buffer` | Alerts buffered during quiet hours | `subscriber_id` (FK), `alert_id`, `buffered_at` |
| `meta` | Key-value store | `key` (PK), `value` — stores: `last_polled_at`, `last_sports_polled_at`, `admin_health`, `source_health` |

`alert_cache` is rebuilt each poll cycle to match currently active alerts. `processed_alerts` is additive and expires entries as alerts go stale.

---

## 🔄 Data flow

```
RMV / DWD / Polizei / Autobahn / Baustellen / Strike / Events / Sports APIs
         │
         ▼
    pollers.py           ← fetch(), returns list[Alert] (German)
         │
         ▼
       db.py             ← sync_alert_cache() — translate + write to alert_cache
         │               ← expire_processed_alerts()
         ▼
    pipeline.py          ← get_unseen_alerts() → cold-start guard → notify → mark_seen
         │
    notifications.py     ← Telegram channel post / ntfy push / subscriber DMs
         │
       db.py             ← set_meta("last_polled_at")

Status page:
    web/app.py GET /api/status → db.get_status_json() → alert_cache → browser

Bot interaction:
    Telegram → Caddy → notifier:8443 → bot.py → db.py (subscribers, sent_alerts, quiet_buffer)
```

---

## ⚙️ Configuration

`config.yaml` is the single non-secret configuration source. It lives in the `data/` volume and is editable at runtime. `.env` holds secrets only.

`entrypoint.sh` reads `config.yaml` once at container start to generate the crontab. Config changes to poll schedule or quiet hours require a container restart; all other keys are read fresh on each `main.py` invocation.

See [self-hosting.md](self-hosting.md) for the full configuration reference.
