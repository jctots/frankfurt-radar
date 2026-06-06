import json
from pathlib import Path
from unittest.mock import MagicMock

import feedparser
import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _mock_response(data: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    return resp


# ── RMVPoller ────────────────────────────────────────────────────────────────

class TestRMVPoller:
    def test_filters_out_non_frankfurt_region(self, mocker):
        from pollers import RMVPoller
        fixture = json.loads((FIXTURES_DIR / "rmv_him_response.json").read_text(encoding="utf-8"))
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = RMVPoller(api_key="key", services={}).fetch()

        # Fixture has 2 messages: Frankfurt (passes) and Darmstadt (filtered)
        assert len(alerts) == 1
        assert alerts[0].id == "HIM_FPLAN_001"

    def test_alert_fields_populated(self, mocker):
        from pollers import RMVPoller
        fixture = json.loads((FIXTURES_DIR / "rmv_him_response.json").read_text(encoding="utf-8"))
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alert = RMVPoller(api_key="key", services={}).fetch()[0]

        assert alert.source == "rmv"
        assert alert.service == "S-Bahn"
        assert "S1" in alert.lines
        assert "S2" in alert.lines
        # HTML stripped from body
        assert "<b>" not in alert.body
        assert alert.valid_until == "2026-06-04T20:00:00+00:00"
        # Location extracted from edge
        assert alert.lat == 50.107
        assert alert.lon == 8.664
        assert alert.location_label is not None
        assert "Frankfurt Hbf" in alert.location_label
        assert "Frankfurt S" in alert.location_label

    def test_service_filter_drops_wrong_line(self, mocker):
        from pollers import RMVPoller
        fixture = json.loads((FIXTURES_DIR / "rmv_him_response.json").read_text(encoding="utf-8"))
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        # Only allow S3 — the fixture has S1/S2 so nothing should pass
        alerts = RMVPoller(api_key="key", services={"sbahn": ["S3"]}).fetch()
        assert len(alerts) == 0

    def test_service_filter_passes_matching_line(self, mocker):
        from pollers import RMVPoller
        fixture = json.loads((FIXTURES_DIR / "rmv_him_response.json").read_text(encoding="utf-8"))
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = RMVPoller(api_key="key", services={"sbahn": ["S1"]}).fetch()
        assert len(alerts) == 1

    def test_request_failure_returns_empty(self, mocker):
        import requests as req_lib
        from pollers import RMVPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        alerts = RMVPoller(api_key="key", services={}).fetch()
        assert alerts == []


# ── DWDPoller ─────────────────────────────────────────────────────────────────

class TestDWDPoller:
    def test_severity_threshold_filters_minor(self, mocker):
        from pollers import DWDPoller
        fixture = json.loads((FIXTURES_DIR / "dwd_brightsky_response.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        # min_severity=2 (moderate) — fixture has severe (passes) and minor (filtered)
        alerts = DWDPoller(min_severity=2).fetch()
        assert len(alerts) == 1
        assert alerts[0].id == "DWD_WARN_001"

    def test_all_alerts_pass_at_min_severity_1(self, mocker):
        from pollers import DWDPoller
        fixture = json.loads((FIXTURES_DIR / "dwd_brightsky_response.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = DWDPoller(min_severity=1).fetch()
        assert len(alerts) == 2

    def test_english_fields_preferred(self, mocker):
        from pollers import DWDPoller
        fixture = json.loads((FIXTURES_DIR / "dwd_brightsky_response.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alert = DWDPoller(min_severity=1).fetch()[0]
        assert alert.title == "Thunderstorm warning level 3"
        assert "Severe thunderstorms" in alert.body
        assert "Seek shelter" in alert.body

    def test_severity_mapped_to_int(self, mocker):
        from pollers import DWDPoller
        fixture = json.loads((FIXTURES_DIR / "dwd_brightsky_response.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alert = DWDPoller(min_severity=1).fetch()[0]
        assert alert.severity == 3  # "severe" → 3


# ── PolizeiPoller ─────────────────────────────────────────────────────────────

class TestPolizeiPoller:
    def _patched_feed(self):
        xml = (FIXTURES_DIR / "polizei_rss.xml").read_text()
        return feedparser.parse(xml)

    def test_title_prefix_stripped(self, mocker):
        from pollers import PolizeiPoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())

        alerts = PolizeiPoller().fetch()
        # "POL-F: 2026-001 - Frankfurt-Sachsenhausen: Verkehrsunfall" → "Sachsenhausen: Verkehrsunfall"
        assert alerts[0].title == "Sachsenhausen: Verkehrsunfall"

    def test_body_cleaned(self, mocker):
        from pollers import PolizeiPoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())

        alerts = PolizeiPoller().fetch()
        # Presseportal boilerplate should be stripped
        assert "Polizeipräsidium Frankfurt [Newsroom]" not in alerts[0].body
        assert "Original-Content" not in alerts[0].body

    def test_published_at_parsed(self, mocker):
        from pollers import PolizeiPoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())

        alerts = PolizeiPoller().fetch()
        assert alerts[0].published_at is not None
        assert "2026-06-04" in alerts[0].published_at

    def test_source_is_polizei(self, mocker):
        from pollers import PolizeiPoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())

        alerts = PolizeiPoller().fetch()
        assert all(a.source == "polizei" for a in alerts)
