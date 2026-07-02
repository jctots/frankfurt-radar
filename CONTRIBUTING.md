# 🤝 Contributing to Frankfurt Radar

## 🖥️ Running locally

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # fill in RMV_API_KEY at minimum
python main.py                   # one poll cycle
```

Web server:

```bash
cd web
pip install -r requirements.txt
flask --app app run --port 8080
```

## ✅ Running tests

Always run pytest from the activated venv — never against the system Python:

```bash
pip install -r requirements-dev.txt
pytest
```

## 📁 Project structure

| File / directory | Purpose |
|------|---------|
| `main.py` | Poll entry point — fetch, translate, cache, dedup, hand off to the notifier |
| `pollers.py` | `BasePoller` + all source pollers (RMV, DWD, Polizei, Feuerwehr, Autobahn, Baustellen, Strike, static events, OpenLigaDB, Ticketmaster) |
| `pipeline.py` | Deduplication and cold-start guard |
| `translation.py` | Google Translate and LibreTranslate backends + translation variant cache |
| `extraction.py` | Gemini Flash extraction — strike dates/locations, strike dedup, police geocoding |
| `pulse.py` / `pulse_categories.py` | City Pulse — LLM synthesis / deterministic scoring (see [docs/analysis.md](docs/analysis.md)) |
| `radar.py` | Weather radar frame downloader (DWD WMS) |
| `trigger.py` | Poller admin API — poll trigger, config read/patch |
| `notifications.py` | Telegram channel and ntfy send backends |
| `db.py` | SQLite helpers (alerts, subscribers, pulse, API usage, meta) |
| `models.py` | `Alert` dataclass + message formatting |
| `districts.py` | Frankfurt district lookup from coordinates |
| `notifier/` | Bot webhook server, command handlers, subscriber dispatch, quiet hours |
| `web/` | Flask status page, JSON API, admin dashboard |
| `mcp/` | MCP server for AI assistant integration |
| `prompts/` | Editable LLM prompt templates (YAML frontmatter + body) |
| `entrypoint.sh` | Seeds the data volume, generates the crontab, starts the trigger API |
| `city_events.yaml` / `messe_events.yaml` / `sports_events.yaml` | Static event definitions |
| `tests/` | Pytest suite with fixtures |

For the full picture — containers, data flow, database schema — see [docs/architecture.md](docs/architecture.md).

## ➕ Adding a new alert source

1. Subclass `BasePoller` in `pollers.py`
2. Implement `fetch() -> list[Alert]` — return normalized `Alert` objects
3. Register it in the source list in `main.py`
4. Add a config section in the default `config.yaml`
5. Document the data source and its license in `README.md`

The rest of the pipeline (translation, caching, deduplication, notification) is source-agnostic.

## 🔀 Pull requests

- One logical change per PR
- Run `pytest` and confirm the suite passes before submitting
- Run `python main.py` locally and confirm no exceptions
- For new pollers: include a sample fixture and note the data source license
