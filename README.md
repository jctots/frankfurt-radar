# Frankfurt Radar 📡

[![Tests](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml)
[![Docker](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GHCR](https://img.shields.io/badge/ghcr.io-jctots%2Ffrankfurt--radar-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-poller)

Real-time alert service for Frankfurt am Main — transit disruptions, weather warnings, and local police reports, translated to English and delivered to where you are.

**Live service:** [@FrankfurtRadar on Telegram](https://t.me/FrankfurtRadar) · [Status page](https://frankfurt-radar.com)

![Frankfurt Radar demo](docs/assets/demo.gif)

## ⚡ What it does

- Polls RMV (S-Bahn/U-Bahn/tram/bus), DWD weather, and Frankfurt police press releases
- Translates German alerts to English
- Posts to a public Telegram channel and a live status webpage
- Configurable line filters, severity thresholds, and quiet hours
- Self-hostable via Docker Compose

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
| `polling.quiet_hours` | 23–07 | Suppress notifications outside this window |
| `notifier.notify_burst_threshold` | `10` | Cold-start guard — skip notifications if ≥ N new alerts on first run |
| `weather.min_severity` | `1` | 1=minor, 2=moderate, 3=severe, 4=extreme |
| `transport.services` | (all) | Filter by service type and line |
| `police.translate_body` | `false` | `false` = title + link only (safe harbour); `true` = full translated body |

## ⚖️ Legal

- **RMV data**: used under [RMV Open Data terms](https://opendata.rmv.de/). Commercial redistribution requires a separate agreement with RMV.
- **DWD data**: [DL-DE→Zero](https://www.govdata.de/dl-de/zero-2-0) — freely reusable, including commercially.
- **Police press releases**: Presseportal RSS — personal and non-commercial use only (§87g(2) UrhG). The public instance runs with `police.translate_body: false` (title + link only). Full-body translation is available for personal self-hosted deployments.

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📄 License

MIT — see [LICENSE](LICENSE).
