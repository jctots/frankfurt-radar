# 📡 Frankfurt Radar 

[![Tests](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml)
[![Docker](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml)
[![Scan](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GHCR poller](https://img.shields.io/badge/ghcr.io-poller-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-poller)
[![GHCR web](https://img.shields.io/badge/ghcr.io-web-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-web)

Real-time alert service for Frankfurt am Main — transit disruptions, weather warnings, and local police reports, translated to English and delivered to where you are.

🌐 **Website:** [frankfurt-radar.com](https://frankfurt-radar.com)
📱 **Telegram:** [@FrankfurtRadar](https://t.me/FrankfurtRadar)

![Frankfurt Radar demo](docs/assets/demo.gif)

## ⚡ What it does

- Polls RMV (S-Bahn/U-Bahn/tram/bus), DWD weather, and Frankfurt police press releases
- Translates German alerts to English
- Posts to a public Telegram channel and a live status webpage
- Configurable line filters and severity thresholds
- Self-hostable via Docker Compose

## 🖥️ Web interface

- Live alert feed with source, severity, and line filters (persisted per browser)
- Interactive Leaflet map with clustered alert pins
- Dark mode, mobile-optimised layout, browser push notifications

## 🗂️ Data sources

| Source | Feed | License |
|--------|------|---------|
| RMV | HAFAS HIM API (disruptions) | RMV Open Data ToS |
| DWD | BrightSky proxy (weather warnings) | DL-DE→Zero |
| Frankfurt Police | Presseportal RSS | Personal / non-commercial only — see Legal |

## 🏗️ Architecture

```
poller container          web container
──────────────            ─────────────
main.py (cron)  ──────▶  Flask app
  ├── RMVPoller           ├── /           (status page)
  ├── DWDPoller           ├── /api/status (JSON feed)
  └── PolizeiPoller       └── /api/poll   (manual trigger)
        │
        ▼
  SQLite (radar.db) — shared volume
        │
   notifier backend      translator backend
   (Telegram / ntfy)     (Google / LibreTranslate)
```

See [docs/architecture.md](docs/architecture.md) for a full breakdown of each component, the database schema, and the alert data flow.

## 🐳 Self-hosting

**Prerequisites:** Docker, Docker Compose, RMV API key

```bash
git clone https://github.com/jctots/frankfurt-radar
cd frankfurt-radar
cp .env.example .env   # fill in your keys
docker compose up -d
```

On first start, `config.yaml` is seeded to the data volume — edit it at `data/config.yaml` (no container rebuild needed).

### Environment variables (`.env`)

| Variable | Required | Description |
|----------|----------|-------------|
| `RMV_API_KEY` | Yes | RMV Open Data API key |
| `TELEGRAM_BOT_TOKEN` | If `notifier.backend: telegram` | Bot token from @BotFather |
| `GOOGLE_TRANSLATE_API_KEY` | If `translator.backend: google` | Cloud Translation API key |

### Key config options (`data/config.yaml`)

| Key | Default | Description |
|-----|---------|-------------|
| `notifier.backend` | `telegram` | `telegram` or `ntfy` |
| `translator.backend` | `libretranslate` | `google` or `libretranslate` |
| `polling.interval_minutes` | `10` | Poll frequency (10–60 min) |
| `notifier.notify_burst_threshold` | `10` | Cold-start guard — skip notifications if ≥ N new alerts on first run |
| `weather.min_severity` | `1` | 1=minor, 2=moderate, 3=severe, 4=extreme |
| `transport.services` | (all) | Filter by service type and line |
| `police.translate_body` | `false` | `false` = title + link only (safe harbour); `true` = full translated body |

## 🔒 Security & Privacy

Frankfurt Radar is built with security and privacy in mind:

- The status page collects no personal data — no cookies, no analytics, no account required
- Periodic security audits covering infrastructure hardening, OWASP Top 10, and dependency CVE scanning
- Weekly automated `pip audit` and `gitleaks` secret scanning (CI badges above)

See [SECURITY.md](SECURITY.md) for the full security policy and how to report a vulnerability.

## ⚖️ Legal

- **RMV data**: used under [RMV Open Data terms](https://opendata.rmv.de/). Commercial redistribution requires a separate agreement with RMV.
- **DWD data**: [DL-DE→Zero](https://www.govdata.de/dl-de/zero-2-0) — freely reusable, including commercially.
- **Police press releases**: Presseportal RSS — personal and non-commercial use only (§87g(2) UrhG). The public instance runs with `police.translate_body: false` (title + link only). Full-body translation is available for personal self-hosted deployments.

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

MIT — see [LICENSE](LICENSE).

## ☕ Support

Frankfurt Radar is free and ad-free. If it saves you a missed train or a soaked commute, a coffee helps keep the server running.

[![Support on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/jctots)
