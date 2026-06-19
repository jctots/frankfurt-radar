# 📡 Frankfurt Radar

[![Tests](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml)
[![Docker](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml)
[![Scan](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GHCR poller](https://img.shields.io/badge/ghcr.io-poller-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-poller)
[![GHCR web](https://img.shields.io/badge/ghcr.io-web-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-web)

Real-time alert service for Frankfurt am Main — transit disruptions, weather warnings, road closures, police reports, festivals, and sports, translated to English and delivered via a live status page, Telegram channel, or personalized Telegram bot.

**Website:** [frankfurt-radar.com](https://frankfurt-radar.com)
**Telegram channel:** [@FrankfurtRadar](https://t.me/FrankfurtRadar)
**Telegram bot:** [@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot)

![Frankfurt Radar demo](docs/assets/demo.gif)

## ⚡ Features

### 📊 Seven data sources

Frankfurt Radar aggregates alerts from public German data feeds, translates them to English, and delivers them in near real-time.

| Source | What it covers | Feed |
|--------|---------------|------|
| **RMV Transit** | S-Bahn, U-Bahn, Tram, Bus, Regional disruptions | HAFAS HIM API |
| **DWD Weather** | Weather warnings from minor to extreme severity | BrightSky (DWD proxy) |
| **Police** | Frankfurt police press releases | Presseportal RSS |
| **Autobahn** | Highway incidents and closures (A3, A5, A66, etc.) | Autobahn API |
| **City Roads** | Frankfurt road construction and closures | City of Frankfurt WFS |
| **Festivals** | Local events with dates, locations, and images | Curated YAML |
| **Sports** | Eintracht Frankfurt home games, Deutsche Bank Park events | OpenLigaDB, Ticketmaster |

All alerts are translated from German to English via Google Cloud Translation or a self-hosted LibreTranslate instance. DWD weather warnings arrive pre-translated from BrightSky.

### 🖥️ Live status page

The web interface at [frankfurt-radar.com](https://frankfurt-radar.com) provides a real-time overview of all active alerts.

- **Alert feed** with source, severity, service, and line filters — filter state persisted per browser
- **Interactive map** with clustered alert markers (Leaflet + CartoDB/OSM tiles)
- **Weather radar** playback — observation and forecast frames animated on the map
- **Dark mode** toggle, persisted in browser
- **Mobile-optimized** layout — full-height alert list with tap-to-map overlay
- **Search** — real-time text filtering across all alerts
- **Browser notifications** via the Web Push API (opt-in, no server-side storage)
- **Future events** toggle to show/hide upcoming festivals and sports
- **Long-running disruptions** collapsed into an accordion for older alerts
- **Cleared alerts** section showing recently resolved alerts (7-day retention)
- **Pulse indicator** — live green/yellow/red dot showing system health
- **Rotating ticker** — animated headline bar on desktop

No cookies, no accounts, no personal data collected. Anonymous usage analytics via self-hosted Umami (cookie-free, no IP storage).

### 📢 Telegram channel

[@FrankfurtRadar](https://t.me/FrankfurtRadar) — a public channel that receives all alerts, unfiltered. Follow it for a simple, zero-configuration feed.

### 🤖 Telegram bot — personalized alerts

[@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot) — a bot that delivers filtered alerts directly to your DMs, tailored to your commute and interests. You can also search active alerts interactively using `/search`.

**Personalization options:**

- **Source filters** — enable/disable each of the 7 alert sources
- **Transport service filter** — S-Bahn, U-Bahn, Tram, Bus, Regional
- **Line filter** — specific lines (e.g. S3, S5, U4) or all lines
- **Weather severity** — all warnings, moderate+, severe+, or extreme only
- **Autobahn filter** — select specific highways (A3, A5, A66, A661, etc.)
- **Road closure filter** — full closures, partial closures, or both
- **Quiet hours** — buffer alerts overnight and receive a morning briefing at your chosen wake-up time

**Bot commands:**

| Command | Description |
|---------|-------------|
| `/start` | Set up or update personalized alerts |
| `/settings` | Edit your alert preferences |
| `/mystatus` | View your current settings and subscription status |
| `/search` | Search active alerts by keyword (e.g. `/search tram 12`) |
| `/help` | Usage guide and available commands |
| `/stop` | Pause alerts (keeps your settings) |
| `/deletedata` | Permanently delete all your data (GDPR) |

See the [User Guide](docs/user-guide.md) for a full walkthrough of the bot onboarding flow, quiet hours, and morning briefings.

### 🌐 Translation

All German-language alerts are automatically translated to English before delivery. Two pluggable backends:

| Backend | Use case |
|---------|----------|
| Google Cloud Translation | Production — used on the public instance |
| LibreTranslate | Self-hosting — no API key needed for your own instance |

DWD weather warnings arrive pre-translated from BrightSky and skip the translation step.

## 🏗️ Architecture

Frankfurt Radar runs as Docker containers sharing a SQLite database via a named volume. An optional MCP server provides AI assistant integration.

```
poller (cron)              notifier (webhook)        web (Flask/gunicorn)
──────────────             ──────────────────        ────────────────────
main.py                    bot.py                    app.py
├── RMVPoller              ├── /start, /settings     ├── /           (status page)
├── DWDPoller              ├── /mystatus, /help      ├── /api/status (JSON feed)
├── PolizeiPoller          ├── /stop, /deletedata    ├── /api/radar  (radar frames)
├── AutobahnPoller         ├── /status (admin)       ├── /legal
├── BaustellenPoller       ├── /alerts (admin)       ├── /privacy
├── StaticEventsPoller     ├── /poll   (admin)       └── /security
├── StaticSportsPoller     └── subscriber dispatch
├── OpenLigaPoller              │
└── TicketmasterPoller          │
       │                        │
       ▼                        ▼
  SQLite (radar.db) — shared volume
       │
  translator backend
  (Google / LibreTranslate)
```

The poller fetches alerts on a configurable cron schedule (default: every 2 minutes), translates them, and writes to the database. The notifier handles Telegram bot webhooks and dispatches personalized alerts to subscribers. The web container serves the status page and API — read-only, no API keys. The optional MCP server (enabled with `--profile mcp`) exposes alerts to AI assistants like Claude Code via SSE.

See [docs/architecture.md](docs/architecture.md) for the full technical breakdown: database schema, alert pipeline, data flow, and configuration system.

## 🚀 Quick start

### 🌍 Use the hosted instance

No setup needed — just open the website or join the Telegram channel:

1. **Website:** [frankfurt-radar.com](https://frankfurt-radar.com)
2. **Telegram channel:** join [@FrankfurtRadar](https://t.me/FrankfurtRadar) for all alerts
3. **Personalized alerts:** message [@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot) and send `/start`

### 🐳 Self-host your own instance

Frankfurt Radar is open source and designed to be self-hosted via Docker Compose.

```bash
git clone https://github.com/jctots/frankfurt-radar
cd frankfurt-radar
cp .env.example .env   # fill in your API keys
docker compose up -d
```

On first start, `config.yaml` is seeded to the data volume — edit it at `data/config.yaml` (no container rebuild needed).

See [docs/self-hosting.md](docs/self-hosting.md) for the full setup guide, environment variables, configuration reference, and Telegram bot deployment.

## 🔒 Security and privacy

Frankfurt Radar is built with privacy by design:

- The status page collects **no personal data** — no cookies, no accounts
- Anonymous analytics via self-hosted [Umami](https://umami.is/) (cookie-free, no IP storage)
- The Telegram bot stores only your **chat ID and preferences** — no name, username, or message content
- **GDPR compliant** — send `/deletedata` to permanently erase all stored data
- All data stored and processed within the **EU** (Hetzner Frankfurt)
- Weekly automated `pip audit` and `gitleaks` secret scanning (CI badges above)
- Periodic security audits covering infrastructure hardening, OWASP Top 10, and dependency CVEs

Full details: [PRIVACY.md](PRIVACY.md) | [SECURITY.md](SECURITY.md)

## ⚖️ Data sources and licensing

| Source | License | Notes |
|--------|---------|-------|
| RMV | [RMV Open Data ToS](https://opendata.rmv.de/) | Commercial redistribution requires a separate agreement |
| DWD | [DL-DE-Zero](https://www.govdata.de/dl-de/zero-2-0) | Freely reusable, including commercially |
| Presseportal (Police) | Non-commercial only (§87g(2) UrhG) | RSS summary only; links to original press release |
| Autobahn API | [Autobahn API ToS](https://autobahn.api.bund.dev/) | Public federal data |
| City of Frankfurt WFS | Public municipal data | Road construction and closures |
| OpenLigaDB | [CC BY-SA 4.0](https://www.openligadb.de/) | Bundesliga match data |
| Ticketmaster | [Ticketmaster API ToS](https://developer.ticketmaster.com/) | Optional; Deutsche Bank Park events |

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local development setup, project structure, and how to add new alert sources.

## 📄 License

MIT — see [LICENSE](LICENSE).

## ☕ Support

Frankfurt Radar is free and ad-free. If it saves you a missed train or a soaked commute, a coffee helps keep the server running.

[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/jctots)
