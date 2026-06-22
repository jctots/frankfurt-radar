# 📖 User Guide

This guide covers how to use Frankfurt Radar — the website, the Telegram channel, and the personalized Telegram bot.

## 📡 Ways to receive alerts

Frankfurt Radar delivers alerts through three channels. You can use any combination.

| Channel | What you get | Setup |
|---------|-------------|-------|
| **Website** ([frankfurt-radar.com](https://frankfurt-radar.com)) | All alerts on a live map and feed, with filters | None — open the page |
| **Telegram channel** ([@FrankfurtRadar](https://t.me/FrankfurtRadar)) | All alerts, unfiltered | Join the channel |
| **Telegram bot** ([@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot)) | Personalized alerts filtered to your preferences | Message the bot — see below |

## 🖥️ Website

### 📋 Alert feed

The main page shows all active alerts in a scrollable feed. Each alert shows its source, title, timestamp, and affected lines (for transit alerts).

**Filters** — use the filter bar at the top to narrow the feed:

- **Source toggles** — show/hide alerts by source (Transit, Weather, Police, Strikes, Roads, Events, Sports)
- **Service dropdown** — filter transit alerts by service type (S-Bahn, U-Bahn, Tram, Bus, Regional)
- **Severity dropdown** — filter weather alerts by severity level
- **Lines popup** — filter by specific transit lines
- **Search box** — real-time text search across all alerts
- **Future events toggle** — show/hide upcoming festivals and sports

Filter selections are saved in your browser and restored on your next visit.

### 🗺️ Map

Alerts with location data appear as markers on an interactive map. Markers are clustered when zoomed out — click a cluster to expand. Weather warnings appear as a floating panel (city-wide, no point location).

### 📊 City Pulse

The City Pulse overlay appears on the map when you're at the default view (zoom level 12, centered on Frankfurt). It shows an AI-generated situational summary updated every hour, with category trends and an actionable recommendation.

- **Summary** — a concise synthesis of active alerts, highlighting what's new, what's worsening, and how different alerts relate
- **Trends** — five categories (Weather, Transport, Roadworks, Incidents, Events) shown in a compact grid with directional arrows
- **Recommendation** — a proactive suggestion: alternative routes during disruptions, or events worth visiting when conditions are good

The overlay auto-hides when you pan or zoom the map, and reappears when you return to the default view. Close it with the X button — it won't reopen until you navigate away and back. The City Pulse button above the search bar shows category trends at a glance and resets the view when clicked.

### 🌧️ Weather radar

The radar overlay shows precipitation observations and forecasts animated over the map. Use the playback controls to scrub through frames.

### 🌙 Dark mode

Toggle dark mode with the button in the header. Your preference is saved in your browser.

### 📱 Mobile

On mobile, the alert feed takes the full screen. Tap an alert with a location to open the map as a full-screen overlay. Tap the X or swipe to return to the feed.

### 🔔 Browser notifications

Click the notification bell to enable browser push notifications for new alerts. This uses the Web Push API — permission is stored locally in your browser and never sent to the server.

## 📢 Telegram channel

[@FrankfurtRadar](https://t.me/FrankfurtRadar) is a public channel that receives all alerts, unfiltered. Join it for a simple, zero-configuration feed. No data is stored in Frankfurt Radar systems — Telegram's own privacy policy applies.

If you later set up personalized alerts via the bot, you can leave the channel to avoid duplicates.

## 🤖 Telegram bot

[@frankfurt_radar_bot](https://t.me/frankfurt_radar_bot) delivers filtered alerts directly to your DMs, tailored to your commute and interests.

### 🚀 Getting started

1. Search for **@frankfurt_radar_bot** in Telegram, or tap the link above
2. Send `/start`
3. Tap **"Set up my alerts"** to begin the onboarding wizard

### ⚙️ Setting up your alerts

The bot walks you through each category using buttons — no typing needed (except for specific line names).

#### Source selection

Toggle each alert source on or off, then tap **Done**:

- **Transport** — RMV S-Bahn, U-Bahn, Tram, Bus, Regional disruptions
- **Weather** — DWD weather warnings
- **Police** — Frankfurt police press releases
- **Strikes** — labor strike alerts (ver.di Hessen, hessenschau)
- **Roads** — Autobahn incidents and city road closures
- **Festivals** — local city festival events
- **Sports** — Eintracht Frankfurt and Deutsche Bank Park events

#### Transport filters

If you enabled Transport, you can narrow by:

- **Service type** — S-Bahn, U-Bahn, Tram, Bus, Regional (multi-select)
- **Specific lines** — type line names separated by commas (e.g. `S3, S5, U4, Bus 32`)

#### Road filters

If you enabled Roads:

- **Autobahn** — select specific highways (A3, A5, A45, A60, A66, A67, A480, A648, A661)
- **City roads** — full closures only, partial closures only, or both

#### Quiet hours

Quiet hours buffer alerts overnight and deliver them as a morning briefing when your quiet hours end.

- Choose a preset (e.g. 22:00–07:00) or set custom start/end times
- Quiet hours use the Europe/Berlin timezone
- If no alerts arrived during quiet hours, no briefing is sent

### 💬 How alerts look

**Real-time alert (DM):**

```
🟢 S3/S5: Delays between Frankfurt Süd and Offenbach

Signal failure near Frankfurt Süd. Expect delays
of 10–15 minutes on S3 and S5 until approximately 14:30.

Details ↗
```

**Morning briefing (after quiet hours):**

```
☀️ Good morning! Here's what happened while you were away:

🚇 Transport
• 🟢 S3/S5: Delays between Frankfurt Süd and Offenbach
• 🟢 U4: Service suspended Bockenheimer Warte – Enkheim

⛈️ Weather
• ⌛ Wind warning for Frankfurt — until 09:00 20 Jun

📅 Upcoming
• Museumsuferfest — 26–28 Jun

3 alerts during quiet hours (22:00–07:00)
```

Alert status indicators: **🟢** = ongoing, **⌛** = future (with date/time).

### 🎛️ Commands

| Command | Description |
|---------|-------------|
| `/start` | Set up or update your alert preferences. Your current settings are pre-selected so you can adjust without starting from scratch. |
| `/settings` | Same as `/start` — opens the preference wizard. |
| `/mystatus` | View your current preferences and subscription status. |
| `/search` | Search active alerts by keyword (e.g. `/search tram 12`). Results are paginated with Previous/Next buttons. |
| `/help` | Quick reference of commands, preferences, and quiet hours. |
| `/stop` | Pause alerts. Your preferences are saved — send `/start` to resume. |
| `/deletedata` | Permanently delete all your data (chat ID, preferences, alert history). Cannot be undone. |

### ✏️ Changing your preferences

Send `/settings` at any time. The wizard opens with your current choices pre-selected — tap to toggle what you want to change, then tap Done through each step.

### 🔒 Privacy

The bot stores only your Telegram chat ID (a numeric identifier — not your name or username), your alert preferences, and a log of which alerts were sent to you (for deduplication). No name, username, phone number, or message content is stored.

Send `/deletedata` to permanently erase everything. See [PRIVACY.md](../PRIVACY.md) for full details.

## ❓ FAQ

**Can I receive alerts without setting up preferences?**
Yes — follow the @FrankfurtRadar channel for all alerts, or just visit the website. No bot interaction needed.

**Will I get duplicates from the channel and DMs?**
If you're subscribed to both, yes. After setting up personalized alerts, the bot suggests leaving the channel.

**What happens if I block the bot?**
Your subscription is deactivated. Your preferences are kept — unblock and send `/start` to resume.

**How often are alerts checked?**
Every 2 minutes. There may be a short delay between an alert appearing on the website and arriving in your DMs.

**Can I get alerts in German?**
Not yet — all alerts are translated to English.

**How do I delete my data?**
Send `/deletedata` to the bot. This permanently removes your chat ID, preferences, and alert history.
