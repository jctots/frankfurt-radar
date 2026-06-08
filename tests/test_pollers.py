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


# ── AutobahnPoller ────────────────────────────────────────────────────────────

class TestAutobahnPoller:
    def test_warnings_parsed(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        resp_warn = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_warn, resp_empty])

        alerts = AutobahnPoller(roads=["A5"]).fetch()
        assert len(alerts) == 2  # SEQ_WARN_003 (Cologne) filtered by 50 km radius
        assert all(a.source == "autobahn" for a in alerts)
        assert alerts[0].id == "SEQ_WARN_001"
        assert alerts[0].service == "A5"

    def test_coordinates_parsed_lon_lat_order(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        resp_warn = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_warn, resp_empty])

        alert = AutobahnPoller(roads=["A5"]).fetch()[0]
        assert alert.lon == pytest.approx(8.694)
        assert alert.lat == pytest.approx(50.113)

    def test_closure_valid_until_parsed(self, mocker):
        from pollers import AutobahnPoller
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        fixture = json.loads((FIXTURES_DIR / "autobahn_closure.json").read_text())
        resp_closure = _mock_response(fixture)
        mocker.patch("pollers.requests.get", side_effect=[resp_empty, resp_closure])

        alerts = AutobahnPoller(roads=["A3"]).fetch()
        assert len(alerts) == 1
        assert alerts[0].valid_until is not None
        assert "2026-06-08" in alerts[0].valid_until

    def test_204_returns_empty(self, mocker):
        from pollers import AutobahnPoller
        resp = MagicMock()
        resp.status_code = 204
        mocker.patch("pollers.requests.get", return_value=resp)

        alerts = AutobahnPoller(roads=["A5"]).fetch()
        assert alerts == []

    def test_request_failure_returns_empty(self, mocker):
        import requests as req_lib
        from pollers import AutobahnPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        alerts = AutobahnPoller(roads=["A5"]).fetch()
        assert alerts == []

    def test_radius_filter_drops_distant_incidents(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        resp_warn = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_warn, resp_empty])

        # Fixture has 3 warnings: 2 near Frankfurt, 1 near Cologne (~190 km away)
        alerts = AutobahnPoller(roads=["A5"], radius_km=50.0).fetch()
        assert len(alerts) == 2
        assert all(a.id != "SEQ_WARN_003" for a in alerts)

    def test_deduplication_across_roads(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        # Both roads return the same identifier → should deduplicate
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = AutobahnPoller(roads=["A5", "A3"]).fetch()
        ids = [a.id for a in alerts]
        assert len(ids) == len(set(ids))


# ── TicketmasterPoller ────────────────────────────────────────────────────────

class TestTicketmasterPoller:
    def test_events_parsed(self, mocker):
        from pollers import TicketmasterPoller
        fixture = json.loads((FIXTURES_DIR / "ticketmaster_events.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = TicketmasterPoller(api_key="key").fetch()
        assert len(alerts) == 2
        assert all(a.source == "events" for a in alerts)

    def test_alert_fields_populated(self, mocker):
        from pollers import TicketmasterPoller
        fixture = json.loads((FIXTURES_DIR / "ticketmaster_events.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alert = TicketmasterPoller(api_key="key").fetch()[0]
        assert alert.id == "TM_EVT_001"
        assert "Coldplay" in alert.title and "Spheres" in alert.title
        assert alert.url == "https://www.ticketmaster.de/event/coldplay-001"
        assert "Deutsche Bank Park" in alert.body
        assert "2026-06-12" in alert.body
        assert alert.valid_until is not None
        assert "2026-06-12" in alert.valid_until

    def test_coordinates_from_venue(self, mocker):
        from pollers import TicketmasterPoller
        fixture = json.loads((FIXTURES_DIR / "ticketmaster_events.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alert = TicketmasterPoller(api_key="key").fetch()[0]
        assert alert.lat == pytest.approx(50.0687)
        assert alert.lon == pytest.approx(8.6454)

    def test_empty_embedded_returns_empty(self, mocker):
        from pollers import TicketmasterPoller
        mocker.patch("pollers.requests.get", return_value=_mock_response({}))

        alerts = TicketmasterPoller(api_key="key").fetch()
        assert alerts == []

    def test_request_failure_returns_empty(self, mocker):
        import requests as req_lib
        from pollers import TicketmasterPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        alerts = TicketmasterPoller(api_key="key").fetch()
        assert alerts == []
