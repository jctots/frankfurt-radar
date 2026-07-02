# 📡 Frankfurt Radar

[![Tests](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/tests.yml)
[![Docker](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/docker.yml)
[![Scan](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml/badge.svg)](https://github.com/jctots/frankfurt-radar/actions/workflows/scan.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GHCR poller](https://img.shields.io/badge/ghcr.io-poller-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-poller)
[![GHCR web](https://img.shields.io/badge/ghcr.io-web-blue?logo=docker)](https://github.com/jctots/frankfurt-radar/pkgs/container/frankfurt-radar-web)

**Everything happening in Frankfurt right now — in English.**

Frankfurt Radar watches the city's official German-language feeds — transit disruptions, weather warnings, road closures, police reports, fire department incidents, strikes, festivals, trade fairs, and sports — translates everything to English, and delivers it to you in near real-time. Built for expats, travelers, and anyone who doesn't want to parse Beamtendeutsch at 7 a.m.

**Website:** [frankfurt-radar.com](https://frankfurt-radar.com)
**Telegram channel:** [@FrankfurtRadar](https://t.me/FrankfurtRadar)
**Telegram bot:** [@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot)

![Frankfurt Radar demo](docs/assets/demo.gif)

## 🚀 Three ways to use it

| | What you get | Setup |
|---|---|---|
| 🖥️ **[Website](https://frankfurt-radar.com)** | Live map and alert feed with filters, weather radar, and an AI city summary | None — just open it |
| 📢 **[Telegram channel](https://t.me/FrankfurtRadar)** | Every alert, unfiltered, as it happens | Join the channel |
| 🤖 **[Telegram bot](https://t.me/frankfurt_radar_bot)** | Only the alerts *you* care about — your lines, your highways, your neighborhood | Message the bot, send `/start` |

New to Frankfurt Radar? The [User Guide](docs/user-guide.md) walks through all three.

## ⚡ What it covers

### 📊 Ten data sources

| Source | What it covers |
|--------|---------------|
| 🚇 **Transit (RMV)** | S-Bahn, U-Bahn, Tram, Bus, and Regional disruptions |
| ⛈️ **Weather (DWD)** | Official weather warnings, from minor to extreme |
| 🚨 **Police** | Frankfurt police press releases |
| 🔥 **Fire** | Feuerwehr Frankfurt active incidents by district |
| ⚠️ **Autobahn** | Highway closures and roadworks (A3, A5, A66, A661, …) |
| 🚧 **City Roads** | Road construction and closures inside Frankfurt |
| 🪧 **Strikes** | Labor strike alerts affecting transit, retail, and public services |
| 🎉 **Festivals** | City festivals, markets, and parades — with dates and locations |
| 🎪 **Trade Fairs** | Events at Messe Frankfurt |
| ⚽ **Sports** | Eintracht Frankfurt home games and Deutsche Bank Park events |

Everything arriving in German is automatically translated to English before it reaches you.

### 🖥️ Live status page

[frankfurt-radar.com](https://frankfurt-radar.com) shows all active alerts on an interactive map and a filterable feed:

- **Filter** by source, transit service, line, or severity — your selections are remembered
- **Search** across all alerts in real time
- **Weather radar** playback — precipitation observations and forecasts animated on the map
- **Weather overlay** — current conditions, rainfall, and daily high/low forecast at a glance
- **Dark mode**, mobile-optimized layout, and opt-in browser notifications
- **No cookies, no accounts, no personal data** — just open it

### 🏙️ City Pulse — AI situational summary

Instead of making you read forty alerts, City Pulse reads them for you. Every hour, an AI synthesizes all active alerts into a short situational overview: what's really going on, which alerts are connected, and one practical recommendation.

Five categories — Weather, Transport, Roadworks, Incidents, Events — each get a live status and trend, shown right on the map. Bot subscribers can get the pulse delivered by DM at 08:00, 12:00, or 18:00, or fetch it any time with `/pulse`.

Curious how it works? The [methodology page](https://frankfurt-radar.com/pulse-methodology) explains it in plain language, and [docs/analysis.md](docs/analysis.md) has the full technical breakdown.

### 🤖 Personalized alerts via Telegram bot

[@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot) delivers filtered alerts straight to your DMs. A button-based setup wizard lets you pick:

- **Sources** — any combination of the ten sources above
- **Transit** — specific services (S-Bahn, U-Bahn, …) or individual lines (S3, U4, …)
- **Roads** — specific highways, full or partial city closures
- **Location keywords** — get any alert mentioning your neighborhood (e.g. *Bockenheim*), regardless of source
- **Quiet hours** — buffer alerts overnight and get a morning briefing at your wake-up time
- **City Pulse delivery** — the daily AI summary at a time of your choosing

Search active alerts with `/search`, check the city with `/pulse`, and delete everything with `/deletedata` — full command list in the [User Guide](docs/user-guide.md).

### 🧠 Ask your AI assistant

Frankfurt Radar ships a [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) server, so AI assistants like Claude can answer questions such as *"any S-Bahn disruptions right now?"* or *"what's the city pulse?"* using live data. To request an API key for the hosted instance, [open an MCP access request](https://github.com/jctots/frankfurt-radar/issues/new?template=mcp_access.md); setup details are in the [Self-Hosting Guide](docs/self-hosting.md#-mcp-server-ai-integration).

## 🐳 Run your own

Frankfurt Radar is open source and designed to be self-hosted — the same setup that runs [frankfurt-radar.com](https://frankfurt-radar.com), packaged as Docker Compose:

```bash
git clone https://github.com/jctots/frankfurt-radar
cd frankfurt-radar
cp .env.example .env   # fill in your API keys
docker compose up -d
```

The [Self-Hosting Guide](docs/self-hosting.md) covers everything: environment variables, the full configuration reference, the Telegram bot, the admin dashboard, and the MCP server.

## 📚 Documentation

| Document | What's inside |
|----------|---------------|
| [User Guide](docs/user-guide.md) | Using the website, channel, and bot — onboarding, quiet hours, commands |
| [Self-Hosting Guide](docs/self-hosting.md) | Setup, configuration reference, admin dashboard, MCP server |
| [Architecture](docs/architecture.md) | Containers, data flow, database schema, pipeline internals |
| [City Pulse Analysis](docs/analysis.md) | How the AI summary works — scoring, trends, calibration |
| [Telegram Bot Setup](docs/telegram-bot-setup.md) | Creating and wiring up your own bot |

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
| DWD | [DL-DE-Zero](https://www.govdata.de/dl-de/zero-2-0) | Weather warnings (via BrightSky) and radar imagery; freely reusable |
| Presseportal (Police) | Non-commercial only (§87g(2) UrhG) | RSS summary only; links to original press release |
| Feuerwehr Frankfurt | Public Bluesky feed | @feuerwehrffm.bsky.social via AT Protocol |
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
