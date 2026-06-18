# Frankfurt Radar — Telegram Bot User Manual

## Overview

Frankfurt Radar delivers real-time alerts about transit disruptions, weather warnings, road closures, police reports, events, and sports in the Frankfurt area. You can receive alerts two ways:

1. **@FrankfurtRadar channel** — follow the channel for all alerts, unfiltered.
2. **Personalized DMs** — message the bot directly to set up filtered alerts tailored to your commute and interests.

---

## Getting Started

### Step 1: Find the bot

Search for **@FrankfurtRadarBot** in Telegram, or open the link in the @FrankfurtRadar channel description.

### Step 2: Start the conversation

Send `/start` to the bot. You'll see a welcome message:

```
👋 Welcome to Frankfurt Radar!

I'll send you personalized alerts about transit disruptions,
weather warnings, road closures, and more in Frankfurt.

Let's set up what you'd like to receive.

[Set up my alerts →]
```

Tap **"Set up my alerts →"** to begin.

---

## Setting Up Your Alerts

The bot walks you through each category using buttons — no typing needed.

### Source Selection

You'll see toggle buttons for each alert source. Tap to enable/disable:

```
Which alert sources do you want?
Tap to toggle, then tap Done.

[🚇 Transport ✅]  [⛈️ Weather ✅]
[🚨 Police    ✅]  [⚠️ Roads   ✅]
[🎉 Events    ✅]  [⚽ Sports  ✅]

[Done →]
```

### Transport Filters (if enabled)

**Service type:**
```
Which transport services?

[All services]
[S-Bahn]  [U-Bahn]
[Tram]    [Bus]

[Done →]
```

**Line filter:**

After selecting services, you can optionally narrow to specific lines:

```
You selected: S-Bahn, Bus

Want alerts for all lines on these services,
or only specific lines?

[All lines for S-Bahn, Bus]
[Specific lines only]
```

If you tap **"Specific lines only"**, the bot asks you to type them:

```
Type the line names you want, separated by commas.
Examples: S3, S5, Bus 32, Bus M46

You can always update these later with /settings.
```

You type: `S3, S5, Bus 32`

```
Got it! You'll receive alerts for: S3, S5, Bus 32

[Confirm ✅]  [Re-enter ✏️]
```

### Road Filters (if Roads enabled)

**Autobahn:**
```
Which autobahns do you want alerts for?

[All autobahns]
[A3]  [A5]  [A45]
[A60] [A66] [A67]
[A480] [A648] [A661]

[Done →]
```

**City road closures:**
```
Which city road disruptions?

[Full closures only]
[Partial closures only]
[Both]
```

### Weather Severity (if Weather enabled)

```
Minimum weather severity?

[All warnings]        ← includes minor
[Moderate and above]  ← severity 2+
[Severe and above]    ← severity 3+
[Extreme only]        ← severity 4 only
```

### Quiet Hours

```
Do you want quiet hours?
During quiet hours, alerts are saved and
delivered as a morning briefing.

[No quiet hours]
[Yes — 22:00 to 07:00]
[Custom times]
```

If you choose **Custom times**, the bot asks for start and end times:

```
Quiet hours start time?

[20:00] [21:00] [22:00] [23:00]
```

```
Quiet hours end time?

[06:00] [07:00] [08:00] [09:00]
```

### Done

```
✅ You're all set! Here's your setup:

📋 Sources: Transport, Weather, Roads
🚇 Transport: S-Bahn (S3, S5), Tram (all lines)
⛈️ Weather: Moderate and above
🚧 Roads: A5, A66 + full city closures
🔕 Quiet hours: 22:00–07:00

You'll now receive personalized alerts via DM.

Since you're getting filtered alerts here, you can
leave the @FrankfurtRadar channel to avoid duplicates.
Open the channel → ⋮ menu → Leave channel.
```

---

## How Alerts Look

### Real-time alert (DM)

When an alert matches your preferences, you receive a direct message:

```
🚇 S3/S5: Delays between Frankfurt Süd and Offenbach

Ongoing
Signal failure near Frankfurt Süd. Expect delays
of 10–15 minutes on S3 and S5 until approximately
14:30.

Details ↗
```

### Morning briefing (after quiet hours)

If you have quiet hours enabled, alerts that arrived overnight are grouped and sent when your quiet hours end:

```
☀️ Good morning! Here's what happened while
you were away:

🚇 Transport
• S3/S5: Delays between Frankfurt Süd and Offenbach
• U4: Service suspended between Bockenheimer Warte
  and Enkheim

⛈️ Weather
• Wind warning for Frankfurt — until 09:00 UTC

🚧 Roads
• A5: Lane closure near Frankfurt Niederrad — until
  20 Jun

3 alerts during quiet hours (22:00–07:00)
```

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Start or restart the setup wizard. Your current preferences are pre-selected so you can adjust without starting from scratch. |
| `/settings` | Same as `/start` — opens the setup wizard. |
| `/mystatus` | Shows your current preferences and subscription status. |
| `/help` | Shows a quick reference of available commands, how to change preferences, and how quiet hours work. |
| `/stop` | Pauses your alerts. You stop receiving DMs but your preferences are saved. Send `/start` to resume. |
| `/deletedata` | Permanently deletes your data (chat ID, preferences, alert history). This cannot be undone. Required for GDPR compliance. |

---

## Changing Your Preferences

Send `/start` or `/settings` at any time. The setup wizard opens with your current choices pre-selected — tap to toggle what you want to change, then tap **Done**.

Example: you're currently receiving all transport alerts but want to narrow to S-Bahn only:

1. Send `/settings`
2. Tap **"Set up my alerts →"**
3. On source selection, everything is already toggled — tap **Done →**
4. On transport services, tap **S-Bahn** (it highlights), then **Done →**
5. On line filter, tap **All lines** or **Specific lines only** and type your lines
6. Remaining categories keep your previous selections — tap through
7. Done — your preferences are updated immediately

---

## Quiet Hours

When quiet hours are active:
- Alerts that match your preferences are **saved, not sent**
- At the end of your quiet hours, you receive a **morning briefing** with all saved alerts grouped by category
- If no alerts arrived during quiet hours, no briefing is sent

Quiet hours use the **Europe/Berlin** timezone by default.

---

## Frequently Asked Questions

**Can I receive alerts without setting up preferences?**
Yes — follow the @FrankfurtRadar channel for all alerts, unfiltered. No bot interaction needed.

**Will I get duplicate alerts from the channel and DMs?**
If you're subscribed to both, yes. After setting up personalized alerts, the bot suggests leaving the channel to avoid duplicates.

**What happens if I block the bot?**
Your subscription is automatically deactivated. Your preferences are kept — unblock and send `/start` to resume.

**How often are alerts checked?**
Every 2 minutes. There may be a short delay between an alert appearing on the website and arriving in your DMs.

**Can I get alerts in German instead of English?**
Not yet — all alerts are translated to English. German language support may be added in the future.

**How do I delete my data?**
Send `/deletedata`. This permanently removes your chat ID, preferences, and alert history. You cannot undo this.
