# Privacy Policy — Frankfurt Radar

**Last updated:** 2026-06-18

## Who we are

Frankfurt Radar is a privately operated open-source service hosted on a Hetzner VPS in Frankfurt, Germany (EU jurisdiction).
For operator details, see the [Impressum](/legal#impressum) on the deployed instance.

## What data we collect

### Status page (frankfurt-radar.com)

No personal data is collected. No cookies are set.

Anonymous, cookie-free usage analytics are collected via a self-hosted [Umami](https://umami.is/) instance running on the same EU server (Hetzner Frankfurt, Germany). Umami does not use cookies, does not store IP addresses, and cannot identify individual visitors. Anonymised interaction events (e.g. filter use, alert selection) are tracked to understand how the service is used. No analytics data is shared with third parties.

**Legal basis:** legitimate interest (improving the service) — no personal data is processed.

Browser notification permission is stored locally in your browser only — it is never transmitted to our servers.

### Telegram channel (@FrankfurtRadar)

Subscribing to the public channel does not store any data in Frankfurt Radar systems. Telegram's own privacy policy applies.

### Telegram bot (@FrankfurtRadarBot)

When you send `/start` to the bot, we store:

- Your Telegram **chat ID** (a pseudonymous numeric identifier — not your name or username)
- Your **alert preferences** (selected sources, filters, quiet hours configuration)
- A **subscription timestamp**
- **Alert delivery history** — which alerts were sent to you (for deduplication)
- **Conversation state** (temporary — only during onboarding, cleared on completion)

We do not store your name, username, phone number, or any message content.

You can view your stored preferences at any time by sending `/mystatus` to the bot.

## Why we collect it

To deliver personalised alert notifications to your Telegram account.

**Legal basis:** your explicit consent — you initiate contact by sending `/start` to the bot.

## How long we keep it

Until you send `/deletedata` to the bot, which permanently deletes all stored data associated with your chat ID.

## Your rights (GDPR)

You have the right to access, correct, and erase your data.

- **To delete all stored data:** send `/deletedata` to @FrankfurtRadarBot
- **For all other requests:** see [/legal](/legal#impressum) for the operator's contact details

We will respond within 30 days.

## Data transfers

All data is stored and processed within the EU (Hetzner Frankfurt, Germany).

Notifications are delivered via Telegram. Telegram Messenger Inc. operates its own servers — see [telegram.org/privacy](https://telegram.org/privacy).

Alert text is translated to English using the Google Cloud Translation API. Google receives the text of public emergency alerts solely to perform the translation. No personal data is included in translation requests. Google's privacy policy applies: [cloud.google.com/translate](https://cloud.google.com/translate).

## Changes to this policy

Material changes will be noted here with an updated date.

## Contact

See [/legal](/legal#impressum) on the deployed instance for the operator's name and contact email.
