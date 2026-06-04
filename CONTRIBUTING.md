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
| `pollers.py` | `BasePoller` + `RMVPoller`, `DWDPoller`, `PolizeiPoller` |
| `pipeline.py` | Dedup, filter, translate, notify |
| `notifications.py` | Telegram and ntfy backends |
| `translation.py` | Google Translate and LibreTranslate backends |
| `db.py` | SQLite helpers (seen alerts, subscribers, meta) |
| `models.py` | `Alert` dataclass |
| `entrypoint.sh` | Generates crontab dynamically from `config.yaml` |
| `web/app.py` | Flask API and status page server |
| `web/templates/index.html` | Single-page status UI |

## Adding a new alert source

1. Subclass `BasePoller` in `pollers.py`
2. Implement `fetch() -> list[Alert]` — return normalised `Alert` objects
3. Register it in the source list in `pipeline.py`
4. Document the data source and its license in `README.md`

## Pull requests

- One logical change per PR
- Run `python main.py --mode poll` locally and confirm no exceptions before submitting
- For new pollers: include a sample fixture and note the data source license
