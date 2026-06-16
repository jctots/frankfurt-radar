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
        assert "--mode" in args
        assert "poll" in args

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


class TestUmamiVisits:
    def _write_web_config(self, **extra):
        cfg = {"web": {"umami_website_id": "site-123", **extra}}
        _data_dir.mkdir(parents=True, exist_ok=True)
        (_data_dir / "config.yaml").write_text(yaml.dump(cfg))

    def test_not_configured_without_credentials(self, mocker):
        self._write_web_config()
        mocker.patch.object(web_app, "UMAMI_INTERNAL_URL", "http://umami:3000")
        mocker.patch.object(web_app, "UMAMI_USERNAME", "")
        mocker.patch.object(web_app, "UMAMI_PASSWORD", "")

        result = web_app._cmd_visits()
        assert "not configured" in result
        assert "UMAMI_USERNAME" in result

    def test_logs_in_with_username_password(self, mocker):
        self._write_web_config()
        mocker.patch.object(web_app, "UMAMI_INTERNAL_URL", "http://umami:3000")
        mocker.patch.object(web_app, "UMAMI_USERNAME", "admin")
        mocker.patch.object(web_app, "UMAMI_PASSWORD", "secret")
        mocker.patch.object(web_app, "_umami_token", None)

        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"token": "abc123"}
        stats_resp = MagicMock(status_code=200)
        stats_resp.json.return_value = {"visits": {"value": 10}, "visitors": {"value": 5}}
        active_resp = MagicMock(status_code=200)
        active_resp.json.return_value = [{"x": 2}]

        mock_post = mocker.patch("app.http_requests.post", return_value=login_resp)
        mocker.patch("app.http_requests.get", side_effect=[stats_resp, active_resp])

        result = web_app._cmd_visits()

        mock_post.assert_called_once_with(
            "http://umami:3000/api/auth/login",
            json={"username": "admin", "password": "secret"},
            timeout=10,
        )
        assert "Visits: 10" in result
        assert "Unique visitors: 5" in result
        assert "Active now: 2" in result

    def test_handles_flat_stats_response_shape(self, mocker):
        # Self-hosted Umami (Prisma/v2 backend) returns bare numbers instead
        # of the older {"value": N} nesting — confirmed against a live
        # instance: {"pageviews":109,"visitors":13,"visits":63,...}.
        self._write_web_config()
        mocker.patch.object(web_app, "UMAMI_INTERNAL_URL", "http://umami:3000")
        mocker.patch.object(web_app, "UMAMI_USERNAME", "admin")
        mocker.patch.object(web_app, "UMAMI_PASSWORD", "secret")
        mocker.patch.object(web_app, "_umami_token", "cached-token")

        stats_resp = MagicMock(status_code=200)
        stats_resp.json.return_value = {"pageviews": 109, "visitors": 13, "visits": 63, "bounces": 37, "totaltime": 21955}
        active_resp = MagicMock(status_code=200)
        active_resp.json.return_value = []

        mocker.patch("app.http_requests.get", side_effect=[stats_resp, active_resp])

        result = web_app._cmd_visits()
        assert "Visits: 63" in result
        assert "Unique visitors: 13" in result

    def test_retries_login_on_401(self, mocker):
        self._write_web_config()
        mocker.patch.object(web_app, "UMAMI_INTERNAL_URL", "http://umami:3000")
        mocker.patch.object(web_app, "UMAMI_USERNAME", "admin")
        mocker.patch.object(web_app, "UMAMI_PASSWORD", "secret")
        mocker.patch.object(web_app, "_umami_token", "stale-token")

        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"token": "fresh-token"}
        mocker.patch("app.http_requests.post", return_value=login_resp)

        unauthorized = MagicMock(status_code=401)
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = {"visits": {"value": 1}, "visitors": {"value": 1}}
        active_ok = MagicMock(status_code=200)
        active_ok.json.return_value = []

        mocker.patch("app.http_requests.get", side_effect=[unauthorized, ok_resp, active_ok])

        result = web_app._cmd_visits()
        assert "Visits: 1" in result
