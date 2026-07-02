import copy
import os
import tempfile
from pathlib import Path

import pytest
import yaml

# Must be set before any module is imported — web/app.py and db.py read DATA_DIR
# at module level (DB_PATH, CONFIG_FILE, init_db()).
_tmp_dir = tempfile.mkdtemp(prefix="fr_test_")
os.environ["DATA_DIR"] = _tmp_dir
os.environ.setdefault("RMV_API_KEY", "test_api_key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test_token")
os.environ.setdefault("GOOGLE_TRANSLATE_API_KEY", "test_google_key")

_default_config = {
    "web": {"allow_manual_poll": True},
    "translator": {"backend": "libretranslate", "libretranslate_url": "http://localhost:5000"},
    "notifier": {
        "backend": "telegram",
        "telegram_channel": "@TestChannel",
        "notify_burst_threshold": 15,
        "notify_throttle_every": 0,
    },
    "police": {"enabled": True},
    "weather": {"enabled": True, "min_severity": 2},
    "transport": {"enabled": True, "services": {}},
}
Path(_tmp_dir, "config.yaml").write_text(yaml.dump(_default_config))

import db as _db  # noqa: E402 — must follow DATA_DIR assignment
_db.init_db()

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_db():
    """Wipe all tables before each test so tests are fully isolated."""
    with _db._conn() as conn:
        conn.execute("DELETE FROM processed_alerts")
        conn.execute("DELETE FROM alert_cache")
        conn.execute("DELETE FROM subscribers")
        conn.execute("DELETE FROM sent_alerts")
        conn.execute("DELETE FROM quiet_buffer")
        conn.execute("DELETE FROM pulse_history")
        conn.execute("DELETE FROM pulse_daily_summary")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM translation_variants")
    yield


@pytest.fixture
def config():
    return copy.deepcopy(_default_config)


@pytest.fixture
def rmv_alert():
    from models import Alert
    return Alert(
        id="HIM_1234",
        source="rmv",
        title="S-Bahn Stoerung",
        body="Aufgrund einer Stoerung kommt es zu Verspaetungen.",
        url=None,
        valid_until="2026-06-04T20:00",
        service="S-Bahn",
        lines=["S1"],
    )


@pytest.fixture
def dwd_alert():
    from models import Alert
    return Alert(
        id="DWD_001",
        source="dwd",
        title="Thunderstorm warning",
        body="Severe thunderstorms expected.",
        url=None,
        valid_until="2026-06-04T18:00:00Z",
        service=None,
        severity=3,
    )


@pytest.fixture
def events_alert():
    from models import Alert
    return Alert(
        id="city-event-2026-schweizer-strassenfest",
        source="events",
        title="Schweizer Straßenfest",
        body="Annual street festival in Sachsenhausen.",
        url=None,
        valid_from="2026-06-19T00:00:00+00:00",
        valid_until="2026-06-22T23:59:00+00:00",
        service=None,
        lat=50.0970,
        lon=8.6840,
    )


@pytest.fixture
def polizei_alert():
    from models import Alert
    return Alert(
        id="https://presseportal.de/1234",
        source="polizei",
        title="Sachsenhausen: Verkehrsunfall",
        body="Ein Unfall auf der Brueckenstrasse.",
        url="https://presseportal.de/1234",
        valid_until=None,
        service=None,
        published_at="2026-06-04T10:00:00+00:00",
    )
