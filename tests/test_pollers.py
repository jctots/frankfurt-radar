import json
from datetime import datetime, timezone
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
        assert alert.valid_until == "2026-06-04T18:00:00+00:00"
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

    def test_null_severity_filtered_out(self, mocker):
        from pollers import DWDPoller
        fixture = {
            "warnings": [{
                "id": "DWD_NO_SEV",
                "event": "THUNDERSTORM",
                "headline": "Unwetterwarnung",
                "description_en": "Severe storms.",
                "instruction_en": "Seek shelter.",
                # no "severity" key
                "onset": "2026-06-11T10:00:00Z",
                "expires": "2026-06-11T18:00:00Z",
                "lat": 50.1109,
                "lon": 8.6821,
            }]
        }
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = DWDPoller(min_severity=1).fetch()

        assert all(a.id != "DWD_NO_SEV" for a in alerts)

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

    def test_missing_published_parsed_gives_null_published_at(self, mocker):
        from pollers import PolizeiPoller

        class _Entry:
            def get(self, k, default=None):
                return {
                    "id": "https://presseportal.de/9999",
                    "title": "POL-F: 2026-001 - Frankfurt-Sachsenhausen: Unfall",
                    "link": "https://presseportal.de/9999",
                    "summary": "Ein Unfall.",
                    "content": None,
                    "published_parsed": None,
                }.get(k, default)

        feed = MagicMock()
        feed.bozo = False
        feed.entries = [_Entry()]
        mocker.patch("pollers.feedparser.parse", return_value=feed)

        alerts = PolizeiPoller().fetch()

        assert len(alerts) == 1
        assert alerts[0].published_at is None

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

    def test_coordinates_parsed(self, mocker):
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

    def test_published_at_is_set_to_poll_time(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        resp_warn = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_warn, resp_empty])

        alert = AutobahnPoller(roads=["A5"]).fetch()[0]
        assert alert.published_at is not None

    def test_bis_zum_format_fallback(self, mocker):
        from pollers import AutobahnPoller
        fixture = {
            "warning": [{
                "identifier": "BIS_ZUM_001",
                "title": "A3 Fahrbahninstandsetzung",
                "description": [
                    "Die Baustelle ist zu folgenden Zeiträumen gültig:",
                    "17.06.26 21:00 bis zum 18.06.26 05:00 Uhr.",
                    "(Ende der Gesamtmaßnahme: 19.06.26)",
                ],
                "point": "50.113,8.694",
                "startTimestamp": "",
                "endTimestamp": "",
            }]
        }
        resp = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp, resp_empty])

        alert = AutobahnPoller(roads=["A3"]).fetch()[0]
        assert alert.published_at is not None
        assert alert.valid_until is not None
        assert "2026-06-18" in alert.valid_until

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

    def test_empty_point_gives_null_coordinates(self, mocker):
        from pollers import AutobahnPoller
        fixture = {
            "warning": [{
                "identifier": "NO_POINT_001",
                "title": "A5 Baustelle",
                "description": ["Sperrung"],
                "point": "",
                "startTimestamp": "2026-06-10T08:00:00Z",
                "endTimestamp": "",
            }]
        }
        resp = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp, resp_empty])

        alerts = AutobahnPoller(roads=["A5"]).fetch()

        assert len(alerts) == 1
        assert alerts[0].lat is None
        assert alerts[0].lon is None

    def test_deduplication_across_roads(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        # Both roads return the same identifier → should deduplicate
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = AutobahnPoller(roads=["A5", "A3"]).fetch()
        ids = [a.id for a in alerts]
        assert len(ids) == len(set(ids))


# ── StaticEventsPoller ───────────────────────────────────────────────────────

class TestStaticEventsPoller:
    _NOW = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)   # inside Autumn Dippemess window

    _EVENTS = [
        {
            "name": "Autumn Dippemess",
            "start": "2026-09-12",
            "end": "2026-09-28",
            "location": "Festplatz Ratsweg",
        },
        {
            "name": "Museumsuferfest",
            "start": "2026-08-28",
            "end": "2026-08-30",
            "location": "Museumsufer",
        },
        {
            "name": "Christmas Market",
            "start": "2026-11-23",
            "end": "2026-12-22",
            "location": "Römerberg / Zeil",
        },
        {
            "name": "Buchmesse",
            "start": "2026-10-07",
            "end": "2026-10-11",
            "location": "Messe Frankfurt",
            "url": "https://www.buchmesse.de",
        },
    ]

    def _poller(self, advance_days=7):
        from pollers import StaticEventsPoller
        return StaticEventsPoller(events=self._EVENTS, advance_days=advance_days)

    def _fetch(self, advance_days=7):
        import unittest.mock as mock
        poller = self._poller(advance_days)
        with mock.patch("pollers.datetime") as m:
            m.now.return_value = self._NOW
            m.fromisoformat.side_effect = datetime.fromisoformat
            return poller.fetch()

    def test_active_event_returned(self):
        alerts = self._fetch()
        titles = [a.title for a in alerts]
        assert "Autumn Dippemess" in titles

    def test_past_event_excluded(self):
        alerts = self._fetch()
        titles = [a.title for a in alerts]
        assert "Museumsuferfest" not in titles

    def test_future_event_in_advance_window_returned(self):
        # Buchmesse starts 2026-10-07; _NOW is 2026-09-15 — 22 days away, outside 7-day window
        alerts = self._fetch(advance_days=7)
        assert not any(a.title == "Buchmesse" for a in alerts)
        # With 30-day window it should appear
        alerts_wide = self._fetch(advance_days=30)
        assert any(a.title == "Buchmesse" for a in alerts_wide)

    def test_future_event_outside_window_excluded(self):
        alerts = self._fetch()
        assert not any(a.title == "Christmas Market" for a in alerts)

    def test_source_is_events(self):
        alerts = self._fetch()
        assert all(a.source == "events" for a in alerts)

    def test_location_label_and_dates_set(self):
        alerts = self._fetch()
        dippemess = next(a for a in alerts if a.title == "Autumn Dippemess")
        assert dippemess.location_label == "Festplatz Ratsweg"
        assert "2026-09-11T22:00:00" in dippemess.valid_from
        assert "2026-09-27T22:00:00" in dippemess.valid_until

    def test_url_forwarded(self):
        alerts = self._fetch(advance_days=30)
        buchmesse = next(a for a in alerts if a.title == "Buchmesse")
        assert buchmesse.url == "https://www.buchmesse.de"

    def test_url_none_when_not_configured(self):
        alerts = self._fetch()
        dippemess = next(a for a in alerts if a.title == "Autumn Dippemess")
        assert dippemess.url is None

    def test_published_at_is_set(self):
        alerts = self._fetch()
        assert all(a.published_at is not None for a in alerts)

    def test_valid_until_is_event_end(self):
        alerts = self._fetch()
        dippemess = next(a for a in alerts if a.title == "Autumn Dippemess")
        assert "2026-09-27T22:00:00" in dippemess.valid_until

    def test_malformed_entry_skipped(self):
        from pollers import StaticEventsPoller
        import unittest.mock as mock
        bad_events = [{"name": "No dates"}, *self._EVENTS]
        poller = StaticEventsPoller(events=bad_events, advance_days=7)
        with mock.patch("pollers.datetime") as m:
            m.now.return_value = self._NOW
            m.fromisoformat.side_effect = datetime.fromisoformat
            alerts = poller.fetch()
        assert not any(a.title == "No dates" for a in alerts)

    def test_stable_id(self):
        alerts = self._fetch()
        dippemess = next(a for a in alerts if a.title == "Autumn Dippemess")
        assert dippemess.id == "city-event-2026-autumn-dippemess"


# ── ok flag ───────────────────────────────────────────────────────────────────

class TestPollerOkFlag:
    def test_ok_true_after_successful_rmv_fetch(self, mocker):
        from pollers import RMVPoller
        import json
        from pathlib import Path
        fixture = json.loads((Path(__file__).parent / "fixtures" / "rmv_him_response.json").read_text(encoding="utf-8"))
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        poller = RMVPoller(api_key="key", services={})
        poller.fetch()

        assert poller.ok is True

    def test_ok_false_after_rmv_request_failure(self, mocker):
        import requests as req_lib
        from pollers import RMVPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        poller = RMVPoller(api_key="key", services={})
        poller.fetch()

        assert poller.ok is False

    def test_ok_false_after_dwd_request_failure(self, mocker):
        import requests as req_lib
        from pollers import DWDPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        poller = DWDPoller()
        poller.fetch()

        assert poller.ok is False

    def test_ok_false_after_autobahn_request_failure(self, mocker):
        import requests as req_lib
        from pollers import AutobahnPoller
        mocker.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout"))

        poller = AutobahnPoller(roads=["A3"])
        poller.fetch()

        assert poller.ok is False

    def test_ok_always_true_for_static_events_poller(self):
        from pollers import StaticEventsPoller
        poller = StaticEventsPoller(events=[])
        poller.fetch()

        assert poller.ok is True

    def test_ok_always_true_for_static_sports_poller(self):
        from pollers import StaticSportsPoller
        poller = StaticSportsPoller(events=[])
        poller.fetch()

        assert poller.ok is True
