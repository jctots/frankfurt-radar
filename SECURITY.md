# 🛡️ Security Policy

## 📦 Supported Versions

Only the latest release on `main` is actively maintained. Older releases do not receive security fixes.

| Version | Supported |
|---|---|
| Latest (`main`) | ✅ |
| Older releases | ❌ |

## 🚨 Reporting a Vulnerability

Please report security vulnerabilities **privately** — do not open a public GitHub issue.

**Email:** see [/legal](/legal#impressum) for the operator's contact email  
**Subject line:** `[frankfurt-radar] Security vulnerability`

Response SLA:
- Acknowledgement within 48 hours
- Assessment within 7 days
- Fix for critical issues within 14 days

We will acknowledge your report and keep you updated on progress. Please allow a reasonable time to fix before public disclosure.

## 🔐 Security Protections

**Infrastructure**
- Cloud firewall: only ports 22 (SSH), 80 (HTTP), and 443 (HTTPS) inbound
- SSH: key-only authentication, password login disabled, fail2ban active
- Automated OS security updates via `unattended-upgrades`

**Network**
- Cloudflare proxy: origin IP protected, DDoS mitigation, Bot Fight Mode active
- TLS 1.2 minimum enforced at the edge
- HSTS enabled (`max-age=31536000`)

**Application**
- Administrative endpoints disabled in production
- Security response headers: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`
- No user input accepted on the public status page
- Bot webhook endpoint validated via `X-Telegram-Bot-Api-Secret-Token` header
- User input during bot onboarding (e.g. line names) is parsed, stripped, and length-constrained
- Admin bot commands (`/status`, `/alerts`, `/poll`) gated by `chat_id` allowlist
- Periodic security audits: threat model, infrastructure hardening review, OWASP Top 10, dependency CVE scan

**Supply chain**
- `pip audit` runs weekly via GitHub Actions — results published as CI badge
- `gitleaks` secret scanning on every commit

## 📊 Data Handling

The public status page is read-only and stores no personal data. Anonymous, cookie-free usage analytics are collected via a self-hosted Umami instance (no cookies, no IP storage, no cross-site tracking) — see [PRIVACY.md](PRIVACY.md) for details.

The Telegram bot stores only the subscriber's `chat_id`, alert preferences, and delivery history. No name, email, or other personal data is collected. Subscribers can delete all stored data at any time via the `/deletedata` command.

See [PRIVACY.md](PRIVACY.md) for full details.
