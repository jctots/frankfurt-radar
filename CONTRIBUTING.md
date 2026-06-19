# Contributing to Frankfurt Radar

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # fill in RMV_API_KEY at minimum
python main.py --mode poll
```

Web server:

```bash
cd web
pip install -r requirements.txt
flask --app app run --port 8080
```

## Project structure

| File | Purpose |
|------|---------|
| `main.py` | Entry point — `--mode poll` or `--mode daily` |
| `pollers.py` | `BasePoller` + all source pollers (RMV, DWD, Polizei, Autobahn, Baustellen, Events, Sports) |
| `pipeline.py` | Dedup, filter, translate, notify, cold-start guard |
| `notifications.py` | Telegram (channel + subscriber DMs) and ntfy backends |
| `translation.py` | Google Translate and LibreTranslate backends |
| `db.py` | SQLite helpers (alerts, subscribers, quiet buffer, meta) |
| `models.py` | `Alert` dataclass |
| `bot.py` | Telegram bot command handlers and subscriber onboarding |
| `entrypoint.sh` | Generates crontab dynamically from `config.yaml` |
| `web/app.py` | Flask API, status page, and radar frame server |
| `web/templates/` | Status page, legal, privacy, security, radar test |
| `city_events.yaml` | Static city event definitions |
| `sports_events.yaml` | Static sports event definitions |

## Adding a new alert source

1. Subclass `BasePoller` in `pollers.py`
2. Implement `fetch() -> list[Alert]` — return normalized `Alert` objects
3. Register it in the source list in `main.py`
4. Add a config section in the default `config.yaml`
5. Document the data source and its license in `README.md`

The rest of the pipeline (translation, caching, deduplication, notification) is source-agnostic.

## Pull requests

- One logical change per PR
- Run `python main.py --mode poll` locally and confirm no exceptions before submitting
- For new pollers: include a sample fixture and note the data source license
