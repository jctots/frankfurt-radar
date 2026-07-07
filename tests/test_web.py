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


    def test_status_includes_pulse_key(self):
        resp = _client.get("/api/status")
        data = resp.get_json()
        assert "pulse" in data

    def test_status_pulse_with_data(self):
        import db
        db.store_pulse({
            "generated_at": "2026-06-22T10:00:00Z",
            "summary": "Web test pulse",
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        resp = _client.get("/api/status")
        pulse = resp.get_json()["pulse"]
        assert pulse is not None
        assert pulse["summary"] == "Web test pulse"


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


def _admin_client():
    client = web_app.app.test_client()
    client.post("/admin/login", data={"token": "test_admin_token"})
    return client


class TestWeightReviewRemoved:
    def test_weight_review_route_gone(self):
        resp = _admin_client().post("/api/admin/weight-review")
        assert resp.status_code == 404


class TestReviewPreview:
    def test_returns_estimate(self, mocker):
        mocker.patch("review.reduce.reduce", return_value={"config_versions": ["v1"]})
        mocker.patch("review.reduce.estimate_cost", return_value={"tokens": 100, "eur": 0.001, "model": "gemini-2.5-pro"})

        resp = _admin_client().post("/api/admin/review/preview", json={"days": 7})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["estimate"]["tokens"] == 100
        assert data["config_versions"] == ["v1"]

    def test_requires_admin_session(self):
        resp = web_app.app.test_client().post("/api/admin/review/preview", json={"days": 7})
        assert resp.status_code in (302, 404)

    def test_all_drivers_per_hour_converted_to_none(self, mocker):
        build_digest = mocker.patch("review.reduce.reduce", return_value={"config_versions": []})
        mocker.patch("review.reduce.estimate_cost", return_value={"tokens": 0, "eur": 0.0, "model": "x"})

        _admin_client().post("/api/admin/review/preview", json={"days": 3, "drivers_per_hour": "all"})

        args, kwargs = build_digest.call_args
        assert args[1] is None


class TestReviewRun:
    def test_writes_three_files_and_returns_report(self, mocker, tmp_path):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": []}, {}
        ))

        resp = _admin_client().post("/api/admin/review/run", json={"days": 1, "drivers_per_hour": 0, "prompt_samples": 0})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["report_md"] == "# Report"
        out_dir = Path(web_app.DATA_DIR) / "review_debug"
        assert (out_dir / f"{data['timestamp']}.digest.json").exists()
        assert (out_dir / f"{data['timestamp']}.report.md").exists()
        assert (out_dir / f"{data['timestamp']}.changes.json").exists()
        for f in out_dir.glob("*"):
            f.unlink()


class TestReviewReports:
    def test_lists_reports(self, mocker):
        mocker.patch("review.reviewer.list_reports", return_value=[{"timestamp": "2026-07-01T000000Z"}])
        resp = _admin_client().get("/api/admin/review/reports")
        assert resp.status_code == 200
        assert resp.get_json()["reports"] == [{"timestamp": "2026-07-01T000000Z"}]

    def test_unknown_report_404s(self, mocker):
        mocker.patch("review.reviewer.list_reports", return_value=[])
        resp = _admin_client().get("/api/admin/review/reports/does-not-exist")
        assert resp.status_code == 404


class TestReviewChangesEndpoint:
    def test_session_auth_works(self, mocker, tmp_path):
        changes_path = tmp_path / "changes.json"
        changes_path.write_text('{"timestamp": "t1", "config_versions": [], "changes": []}', encoding="utf-8")
        mocker.patch("review.reviewer.list_reports", return_value=[
            {"timestamp": "t1", "changes_path": str(changes_path)}
        ])
        resp = _admin_client().get("/api/admin/review/changes/t1")
        assert resp.status_code == 200
        assert resp.get_json()["timestamp"] == "t1"

    def test_header_api_key_works_without_session(self, mocker, tmp_path):
        changes_path = tmp_path / "changes.json"
        changes_path.write_text('{"timestamp": "t1", "config_versions": [], "changes": []}', encoding="utf-8")
        mocker.patch("review.reviewer.list_reports", return_value=[
            {"timestamp": "t1", "changes_path": str(changes_path)}
        ])
        resp = web_app.app.test_client().get(
            "/api/admin/review/changes/t1", headers={"X-Admin-Token": "test_admin_token"}
        )
        assert resp.status_code == 200

    def test_wrong_header_token_rejected(self, mocker):
        mocker.patch("review.reviewer.list_reports", return_value=[])
        resp = web_app.app.test_client().get(
            "/api/admin/review/changes/t1", headers={"X-Admin-Token": "wrong"}
        )
        assert resp.status_code == 401

    def test_no_auth_rejected(self):
        resp = web_app.app.test_client().get("/api/admin/review/changes/t1")
        assert resp.status_code == 401

    def test_unknown_timestamp_404s(self, mocker):
        mocker.patch("review.reviewer.list_reports", return_value=[])
        resp = _admin_client().get("/api/admin/review/changes/does-not-exist")
        assert resp.status_code == 404
