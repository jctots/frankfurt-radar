import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# Add web/ to path so `import app` resolves to web/app.py.
# web/app.py itself does sys.path.insert for the parent dir (to import db),
# so db imports work correctly once web/ is on the path.
_web_dir = str(Path(__file__).parent.parent / "web")
if _web_dir not in sys.path:
    sys.path.insert(0, _web_dir)

import app as web_app  # noqa: E402 — must follow sys.path setup

_client = web_app.app.test_client()
_data_dir = Path(web_app.CONFIG_FILE).parent


def _write_config(allow_poll: bool):
    cfg = {"web": {"allow_manual_poll": allow_poll}}
    _data_dir.mkdir(parents=True, exist_ok=True)
    (_data_dir / "config.yaml").write_text(yaml.dump(cfg))


class TestStatusEndpoint:
    def test_returns_200(self):
        resp = _client.get("/api/status")
        assert resp.status_code == 200

    def test_response_has_required_keys(self):
        resp = _client.get("/api/status")
        data = resp.get_json()
        assert "alerts" in data
        assert "updated_at" in data

    def test_alerts_is_list(self):
        resp = _client.get("/api/status")
        assert isinstance(resp.get_json()["alerts"], list)

    def test_cached_alert_appears_in_response(self, mocker):
        import db
        from models import Alert
        mocker.patch("translation.translate_alert", return_value=("Title EN", "Body EN"))
        alert = Alert(
            id="WEB_TEST_001", source="rmv", title="Test", body="",
            url=None, valid_until=None, service="S-Bahn", lines=["S1"],
        )
        cfg = {"translator": {"backend": "libretranslate"}, "police": {"enabled": True}}
        db.sync_alert_cache([alert], cfg)

        resp = _client.get("/api/status")
        ids = [a["id"] for a in resp.get_json()["alerts"]]
        assert "WEB_TEST_001" in ids

    def test_alert_has_searchable_fields(self, mocker):
        """title and body must be present — client-side search queries both."""
        import db
        from models import Alert
        mocker.patch("translation.translate_alert", return_value=("Searchable Title", "Searchable body text"))
        alert = Alert(
            id="WEB_SEARCH_001", source="rmv", title="Searchable Title", body="Searchable body text",
            url=None, valid_until=None, service="S-Bahn", lines=["S1"],
        )
        cfg = {"translator": {"backend": "libretranslate"}, "police": {"enabled": True}}
        db.sync_alert_cache([alert], cfg)

        resp = _client.get("/api/status")
        alert_data = next((a for a in resp.get_json()["alerts"] if a["id"] == "WEB_SEARCH_001"), None)
        assert alert_data is not None
        assert "title" in alert_data and alert_data["title"]
        assert "body" in alert_data


class TestPollEndpoint:
    def test_disabled_returns_403(self):
        _write_config(allow_poll=False)
        resp = _client.post("/api/poll")
        assert resp.status_code == 403
        _write_config(allow_poll=True)  # restore

    def test_enabled_calls_subprocess(self, mocker):
        _write_config(allow_poll=True)
        mock_run = mocker.patch("app.subprocess.run")
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        resp = _client.post("/api/poll")

        mock_run.assert_called_once()
        args = mock_run.call_args.args[0]
        assert str(web_app.MAIN_PY) in args[1]

    def test_subprocess_failure_returns_500(self, mocker):
        _write_config(allow_poll=True)
        mock_run = mocker.patch("app.subprocess.run")
        mock_run.return_value = MagicMock(returncode=1, stderr="RMV_API_KEY not set")

        resp = _client.post("/api/poll")
        assert resp.status_code == 500


class TestIndexPage:
    def test_returns_html(self):
        resp = _client.get("/")
        assert resp.status_code == 200
        assert b"Frankfurt Radar" in resp.data

    def test_search_input_present(self):
        resp = _client.get("/")
        assert b'id="search-input"' in resp.data
        assert b'id="search-clear"' in resp.data
