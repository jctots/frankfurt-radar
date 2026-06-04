# Frankfurt Radar — Architecture

## Overview

Frankfurt Radar is composed of two independently deployable containers — a **poller** and a **web server** — sharing a single SQLite database via a Docker volume.

```
┌───────────────────────────────────────────────────────────────────┐
│                        Docker Compose                             │
│                                                                   │
│  ┌─────────────────────────────────┐  ┌────────────────────────┐ │
│  │  poller (cron)                  │  │  web (Flask/gunicorn)  │ │
│  │                                 │  │                        │ │
│  │  main.py ──▶ pollers.py         │  │  GET /                 │ │
│  │              pipeline.py        │  │  GET /api/status       │ │
│  │              translation.py     │  │  POST /api/poll        │ │
│  │              notifications.py   │  │                        │ │
│  │              db.py (write)       │  │  db.py (read)          │ │
│  └──────────────┬──────────────────┘  └──────────┬─────────────┘ │
│                 │                                │               │
│                 └─────────┐    ┌─────────────────┘               │
│                           ▼    ▼                                  │
│                     radar_data (volume)                           │
│                     ├── radar.db   (SQLite WAL)                   │
│                     └── config.yaml                               │
└───────────────────────────────────────────────────────────────────┘
```

The poller runs on a cron schedule; the web container is always-on. Neither knows about the other — the database is the only coupling point.

---

## Poller container

### Startup (`entrypoint.sh`)

On container start, `entrypoint.sh`:

1. Seeds `data/config.yaml` from the bundled default if none exists.
2. Runs an inline Python script to generate `/etc/cron.d/frankfurt-radar` from `config.yaml` — `polling.interval_minutes`, `daily_hour`, and `quiet_hours` are all read here, not hardcoded.
3. Injects runtime env vars (`RMV_API_KEY`, `TELEGRAM_BOT_TOKEN`, etc.) into the cron environment block so the cron subprocess has them.
4. Runs one immediate poll (`python main.py --mode poll`) before handing off to `cron -f`.

### Alert pipeline (`main.py`)

Each cron invocation calls `main.py` with `--mode poll` or `--mode daily`:

```
main.py
  ├── load config.yaml + env vars
  ├── init_db()              — create tables if absent (idempotent)
  ├── instantiate pollers    — RMVPoller, DWDPoller, PolizeiPoller (config-driven)
  ├── fetch all alerts       — returns list[Alert]
  ├── sync_alert_cache()     — translate + write to alert_cache table
  ├── expire_processed_alerts() — clean up stale dedup entries
  ├── process_alerts()       — dedup, notify (poll mode) or summarise (daily mode)
  └── set_meta("last_polled_at", ...)
```

### Pollers (`pollers.py`)

All sources subclass `BasePoller`:

```python
class BasePoller(ABC):
    def fetch(self) -> list[Alert]: ...
```

| Poller | Source | Notes |
|--------|--------|-------|
| `RMVPoller` | RMV HAFAS HIM API | Frankfurt region filter + optional service/line filter from config |
| `DWDPoller` | BrightSky proxy (DWD data) | English fields pre-translated; severity threshold from config |
| `PolizeiPoller` | Presseportal RSS | 24h window applied in daily mode; title-only in public mode |

All pollers return `list[Alert]`. Adding a new source means subclassing `BasePoller` and registering it in `main.py` — the rest of the pipeline is source-agnostic.

### Alert model (`models.py`)

```python
@dataclass
class Alert:
    id: str                    # stable dedup key
    source: str                # "rmv" | "dwd" | "polizei"
    title: str                 # German, pre-translation
    body: str                  # German, HTML-stripped
    url: Optional[str]
    valid_until: Optional[str] # ISO UTC
    service: Optional[str]     # "S-Bahn" | "U-Bahn" | "Tram" | "Bus" | "Regional"
    lines: list[str]           # affected line codes
    published_at: Optional[str]
    severity: Optional[int]    # 1–4 (DWD only)
    lat, lon: Optional[float]  # map pin (RMV only)
    location_label: Optional[str]
```

### Pipeline: poll mode vs. daily mode (`pipeline.py`)

**Poll mode** — runs every N minutes:

1. `get_unseen_alerts()` — queries `processed_alerts` table; filters already-seen IDs.
2. Cold-start guard — if `len(new_alerts) >= notify_burst_threshold`, mark all seen silently and exit (prevents notification flood on fresh deploy or after long downtime).
3. For each new alert: translate → notify → mark seen. Optional throttle pause every N notifications.

**Daily mode** — runs once per day (ntfy backend only; skipped for Telegram):

Collects all currently active alerts by source, groups into sections, sends a single summary notification, then marks everything seen.

### Translation (`translation.py`)

Two pluggable backends, selected by `translator.backend` in config:

| Backend | Key | Notes |
|---------|-----|-------|
| `libretranslate` | `LIBRE_TRANSLATE_URL` | Self-hosted; no API key for own instance |
| `google` | `GOOGLE_TRANSLATE_API_KEY` | Cloud Translation API v2; used on public instance |

`translate_alert(alert, config)` returns `(en_title, en_body)`. DWD alerts already arrive in English from BrightSky and skip translation.

### Notifications (`notifications.py`)

Two pluggable backends, selected by `notifier.backend` in config:

| Backend | Key | Notes |
|---------|-----|-------|
| `telegram` | `TELEGRAM_BOT_TOKEN` | Posts to channel specified by `notifier.telegram_channel`; HTML parse mode |
| `ntfy` | — | Posts to `notifier.ntfy_url` / `notifier.ntfy_topic` |

The `notify(title, body, url, config)` function is the single call site — backends are resolved inside.

---

## Database (`db.py`)

SQLite at `data/radar.db` with WAL mode. Five tables:

| Table | Purpose |
|-------|---------|
| `processed_alerts` | Deduplication — seen alert IDs, expiry keyed on `valid_until` (or 7-day fallback) |
| `alert_cache` | Translated alerts shown on the status page; synced to the current fetch result each poll |
| `subscribers` | Telegram subscribers for bot DMs (Phase 2a); `chat_id` + JSON preferences |
| `sent_alerts` | Per-subscriber sent log (Phase 2a) |
| `meta` | Key-value store; `last_polled_at` drives the "updated" timestamp on the status page |

`alert_cache` is the status page's source of truth — it is wiped and rebuilt each poll cycle to match exactly what is currently active. `processed_alerts` is additive (dedup log) and expires entries as alerts become stale.

---

## Web container

Flask app served by gunicorn. Three routes:

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | Status page (single-page app, HTML/JS) |
| `/api/status` | GET | JSON: `{updated_at, alerts: [...]}` read from `alert_cache` |
| `/api/poll` | POST | Triggers `python main.py --mode poll` as a subprocess; disabled when `web.allow_manual_poll: false` |

The web container has no API keys — it only reads from the database. Translation happens in the poller and is cached in `alert_cache` before the web layer ever sees an alert.

### Status page (`web/templates/index.html`)

Single-page app with no build step:

- **Desktop**: left-panel alert feed + right-panel Leaflet map
- **Mobile**: full-height alert list; tap → full-screen map overlay (`transform: translateX`)
- Map tiles: CartoDB Dark Matter (dark mode) / OSM Standard (light mode) — no API key
- Alert markers: clustered with `leaflet.markercluster`; DWD alerts rendered as floating panel (city-wide, no point location)
- Filter bar: source toggles, service/severity dropdowns, lines popup — persisted in `localStorage`
- Dark mode preference persisted in `localStorage`; dark mode toggle visible on mobile

---

## Configuration

`config.yaml` is the single non-secret configuration source. It lives in the `data/` volume and is editable at runtime — no rebuild needed. `.env` holds secrets only (`RMV_API_KEY`, `TELEGRAM_BOT_TOKEN`, `GOOGLE_TRANSLATE_API_KEY`).

`entrypoint.sh` reads `config.yaml` once at container start to generate the crontab. Config changes to poll schedule or quiet hours require a container restart; all other config keys (backends, thresholds, filters) are read fresh on each `main.py` invocation.

---

## Data flow summary

```
RMV / DWD / Polizei APIs
         │
         ▼
    pollers.py           ← fetch(), returns list[Alert] (German)
         │
         ▼
       db.py             ← sync_alert_cache() — translate + write to alert_cache
         │               ← expire_processed_alerts()
         ▼
    pipeline.py          ← get_unseen_alerts() → cold-start guard → translate → notify → mark_seen
         │
    notifications.py     ← Telegram channel post / ntfy push
         │
       db.py             ← set_meta("last_polled_at")

Status page poll:
    web/app.py GET /api/status → db.get_status_json() → alert_cache → browser
```
