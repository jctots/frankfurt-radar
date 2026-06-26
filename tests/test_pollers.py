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

    def test_icon_derived_from_headline(self, mocker):
        from pollers import DWDPoller
        fixture = json.loads((FIXTURES_DIR / "dwd_brightsky_response.json").read_text())
        mocker.patch("pollers.requests.get", return_value=_mock_response(fixture))

        alerts = DWDPoller(min_severity=1).fetch()
        thunderstorm, wind = alerts[0], alerts[1]
        assert thunderstorm.icon == "⛈️"
        assert wind.icon == "💨"


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

    def test_published_at_is_none_from_poller(self, mocker):
        from pollers import AutobahnPoller
        fixture = json.loads((FIXTURES_DIR / "autobahn_warning.json").read_text())
        resp_warn = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_warn, resp_empty])

        alert = AutobahnPoller(roads=["A5"]).fetch()[0]
        assert alert.published_at is None

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
        assert alert.published_at is None
        assert alert.valid_until is not None
        assert "2026-06-18" in alert.valid_until

    def test_von_bis_same_day_format_fallback(self, mocker):
        from pollers import AutobahnPoller
        fixture = {
            "closure": [{
                "identifier": "VON_BIS_001",
                "title": "A45 | Seligenstädter Dreieck - Kleinostheim",
                "description": [
                    "Die Baustelle ist zu folgenden Zeiträumen gültig:",
                    "16.06.26 von 20:00 bis 24:00 Uhr",
                    "(Ende der Gesamtmaßnahme: 17.06.26)",
                ],
                "point": "50.006,9.023",
                "startTimestamp": "",
                "endTimestamp": "",
            }]
        }
        resp = _mock_response(fixture)
        resp_empty = MagicMock()
        resp_empty.status_code = 204
        mocker.patch("pollers.requests.get", side_effect=[resp_empty, resp])

        alert = AutobahnPoller(roads=["A45"]).fetch()[0]
        assert alert.valid_from is not None
        assert alert.valid_until is not None
        assert alert.valid_from == "2026-06-16T18:00:00+00:00"
        assert alert.valid_until == "2026-06-16T22:00:00+00:00"

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
        assert "2026-09-28T21:59:00" in dippemess.valid_until

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
        assert "2026-09-28T21:59:00" in dippemess.valid_until

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
        assert dippemess.id == "events-event-2026-autumn-dippemess"


# ── BaustellenPoller ─────────────────────────────────────────────────────────

class TestBaustellenPoller:
    _NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=timezone.utc)

    _ACTIVE = {
        "baustellennummer": "V-2026-00100",
        "name": "Battonnstraße",
        "textlong": "Rohrleitungsarbeiten in der Battonnstraße.",
        "meldung_en": "roadworks",
        "startevent": "2026-01-01T00:00:00Z",
        "endevent": "2027-01-01T00:00:00Z",
        "sperrung": 1,
    }
    _PARTIAL = {
        "baustellennummer": "V-2026-00200",
        "name": "Schweizer Straße",
        "textlong": "Kanalbauarbeiten.",
        "meldung_en": "roadworks",
        "startevent": "2026-06-01T00:00:00Z",
        "endevent": "2026-12-31T00:00:00Z",
        "sperrung": 0,
    }
    _EXPIRED = {
        "baustellennummer": "V-2025-00001",
        "name": "Old Street",
        "textlong": "Abgeschlossene Maßnahme.",
        "meldung_en": "roadworks",
        "startevent": "2025-01-01T00:00:00Z",
        "endevent": "2025-06-01T00:00:00Z",
        "sperrung": 0,
    }
    _MULTIPOLYGON_GEOM = {
        "type": "MultiPolygon",
        "coordinates": [[[[8.690, 50.110], [8.695, 50.110], [8.695, 50.115], [8.690, 50.115], [8.690, 50.110]]]],
    }
    _LINESTRING_GEOM = {
        "type": "LineString",
        "coordinates": [[8.680, 50.100], [8.685, 50.102], [8.690, 50.105]],
    }

    def _fetch(self, features):
        import unittest.mock as mock
        from pollers import BaustellenPoller
        payload = {"features": [{"properties": f, "geometry": None} for f in features]}
        with mock.patch("pollers.requests.get", return_value=_mock_response(payload)):
            with mock.patch("pollers.datetime") as m:
                m.now.return_value = self._NOW
                m.fromisoformat.side_effect = datetime.fromisoformat
                return BaustellenPoller().fetch()

    def test_active_feature_returned(self):
        alerts = self._fetch([self._ACTIVE])
        assert len(alerts) == 1
        assert alerts[0].source == "baustellen"

    def test_expired_feature_filtered(self):
        alerts = self._fetch([self._EXPIRED])
        assert alerts == []

    def test_full_closure_title(self):
        alerts = self._fetch([self._ACTIVE])
        assert alerts[0].title == "Full closure of Battonnstraße"

    def test_partial_closure_title(self):
        alerts = self._fetch([self._PARTIAL])
        assert alerts[0].title == "Partial closure of Schweizer Straße"

    def test_body_is_textlong(self):
        alerts = self._fetch([self._ACTIVE])
        assert alerts[0].body == "Rohrleitungsarbeiten in der Battonnstraße."

    def test_id_uses_baustellennummer(self):
        alerts = self._fetch([self._ACTIVE])
        assert alerts[0].id == "baustellen-V-2026-00100"

    def test_valid_from_valid_until_set(self):
        alerts = self._fetch([self._ACTIVE])
        assert "2026-01-01" in alerts[0].valid_from
        assert "2027-01-01" in alerts[0].valid_until

    def test_null_coordinates_when_no_geometry(self):
        alerts = self._fetch([self._ACTIVE])
        assert alerts[0].lat is None
        assert alerts[0].lon is None

    def test_multipolygon_centroid(self):
        import unittest.mock as mock
        from pollers import BaustellenPoller
        payload = {"features": [{"properties": self._ACTIVE, "geometry": self._MULTIPOLYGON_GEOM}]}
        with mock.patch("pollers.requests.get", return_value=_mock_response(payload)):
            with mock.patch("pollers.datetime") as m:
                m.now.return_value = self._NOW
                m.fromisoformat.side_effect = datetime.fromisoformat
                alerts = BaustellenPoller().fetch()
        assert alerts[0].lat == pytest.approx(50.1125, abs=0.001)
        assert alerts[0].lon == pytest.approx(8.6925, abs=0.001)

    def test_linestring_midpoint(self):
        import unittest.mock as mock
        from pollers import BaustellenPoller
        payload = {"features": [{"properties": self._PARTIAL, "geometry": self._LINESTRING_GEOM}]}
        with mock.patch("pollers.requests.get", return_value=_mock_response(payload)):
            with mock.patch("pollers.datetime") as m:
                m.now.return_value = self._NOW
                m.fromisoformat.side_effect = datetime.fromisoformat
                alerts = BaustellenPoller().fetch()
        assert alerts[0].lat == pytest.approx(50.102, abs=0.001)
        assert alerts[0].lon == pytest.approx(8.685, abs=0.001)

    def test_request_failure_returns_empty(self):
        import requests as req_lib
        import unittest.mock as mock
        from pollers import BaustellenPoller
        with mock.patch("pollers.requests.get", side_effect=req_lib.RequestException("timeout")):
            p = BaustellenPoller()
            alerts = p.fetch()
        assert alerts == []
        assert p.ok is False

    def test_published_at_is_start_time(self):
        alerts = self._fetch([self._ACTIVE])
        assert "2026-01-01" in alerts[0].published_at

    def test_multiple_only_active_returned(self):
        alerts = self._fetch([self._ACTIVE, self._PARTIAL, self._EXPIRED])
        assert len(alerts) == 2
        ids = {a.id for a in alerts}
        assert "baustellen-V-2025-00001" not in ids

    def test_sperrung_filter_full_only(self):
        import unittest.mock as mock
        from pollers import BaustellenPoller
        payload = {"features": [{"properties": self._ACTIVE, "geometry": None},
                                 {"properties": self._PARTIAL, "geometry": None}]}
        with mock.patch("pollers.requests.get", return_value=_mock_response(payload)):
            with mock.patch("pollers.datetime") as m:
                m.now.return_value = self._NOW
                m.fromisoformat.side_effect = datetime.fromisoformat
                alerts = BaustellenPoller(sperrung_filter={1}).fetch()
        assert len(alerts) == 1
        assert alerts[0].title.startswith("Full closure")

    def test_sperrung_filter_partial_only(self):
        import unittest.mock as mock
        from pollers import BaustellenPoller
        payload = {"features": [{"properties": self._ACTIVE, "geometry": None},
                                 {"properties": self._PARTIAL, "geometry": None}]}
        with mock.patch("pollers.requests.get", return_value=_mock_response(payload)):
            with mock.patch("pollers.datetime") as m:
                m.now.return_value = self._NOW
                m.fromisoformat.side_effect = datetime.fromisoformat
                alerts = BaustellenPoller(sperrung_filter={0}).fetch()
        assert len(alerts) == 1
        assert alerts[0].title.startswith("Partial closure")


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
        from pollers import StaticEventsPoller
        poller = StaticEventsPoller(events=[], source="sports")
        poller.fetch()

        assert poller.ok is True


# ── StrikePoller ────────────────────────────────────────────────────────────────

class TestStrikePoller:
    def _patched_feed(self):
        xml = (FIXTURES_DIR / "strike_rss.xml").read_text()
        return feedparser.parse(xml)

    def _patched_page(self):
        return (FIXTURES_DIR / "strike_press_release.html").read_text()

    def _mock_extraction(self):
        return {
            "summary": "ver.di calls a warning strike in Hessian retail on Dec 5-6, 2099.",
            "valid_from": "2099-12-05T00:00:00+01:00",
            "valid_until": "2099-12-06T23:59:00+01:00",
            "location": "Frankfurt und Region",
            "service": "Retail",
            "affected": ["Rewe", "Ikea", "H&M", "Primark"],
        }

    def test_filters_non_frankfurt_entries(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        ids = [a.id for a in alerts]
        assert "https://hessen.verdi.de/presse/pressemitteilungen/++co++strike-002" not in ids

    def test_filters_non_strike_entries(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        ids = [a.id for a in alerts]
        assert "https://hessen.verdi.de/presse/pressemitteilungen/++co++no-strike-003" not in ids

    def test_alert_fields_populated(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.source == "strike"
        assert alert.title == "Am Brückentag: Warnstreiks im hessischen Handel"
        assert "warning strike" in alert.body.lower()
        assert alert.url == "https://hessen.verdi.de/presse/pressemitteilungen/++co++strike-001"
        assert alert.service == "Retail"
        assert alert.location_label == "Frankfurt und Region"
        assert alert.valid_from is not None
        assert alert.valid_until is not None

    def test_source_is_strike(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        assert all(a.source == "strike" for a in alerts)

    def test_published_at_parsed(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        assert alerts[0].published_at is not None
        assert "2026-06-03" in alerts[0].published_at

    def test_feed_parse_failure_sets_ok_false(self, mocker):
        from pollers import StrikePoller
        bad_feed = MagicMock()
        bad_feed.bozo = True
        bad_feed.bozo_exception = Exception("parse error")
        bad_feed.entries = []
        mocker.patch("pollers.feedparser.parse", return_value=bad_feed)

        poller = StrikePoller(feeds=["https://fake.test/rss"])
        alerts = poller.fetch()

        assert alerts == []
        assert poller.ok is False

    def test_multiple_feeds_deduplicates(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake1.test/rss", "https://fake2.test/rss"], max_age_days=365).fetch()
        ids = [a.id for a in alerts]
        assert len(ids) == len(set(ids))

    def test_max_age_filters_old_entries(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=0).fetch()
        assert len(alerts) == 0

    def test_llm_extraction_populates_dates_and_location(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=self._mock_extraction())

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        alert = alerts[0]
        assert "2099-12-04" in alert.valid_from
        assert "2099-12-06" in alert.valid_until
        assert alert.location_label == "Frankfurt und Region"

    @pytest.mark.parametrize("valid_from,expected_until_date", [
        ("2099-07-15T04:00:00+02:00", "2099-07-15T21:59"),  # CEST: 23:59+02 = 21:59 UTC
        ("2099-12-05T00:00:00+01:00", "2099-12-05T22:59"),  # CET:  23:59+01 = 22:59 UTC
    ])
    def test_valid_until_fallback_respects_dst(self, mocker, valid_from, expected_until_date):
        from pollers import StrikePoller
        extraction = self._mock_extraction()
        extraction["valid_from"] = valid_from
        del extraction["valid_until"]
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value=extraction)

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        alert = alerts[0]
        assert alert.valid_from is not None
        assert alert.valid_until is not None
        assert expected_until_date in alert.valid_until
        assert datetime.fromisoformat(alert.valid_from) <= datetime.fromisoformat(alert.valid_until)

    def test_llm_extraction_fallback_on_failure(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value={})

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.source == "strike"
        assert alert.valid_from is None
        assert alert.valid_until is None
        assert alert.location_label is None

    def test_not_a_strike_entry_skipped(self, mocker):
        from pollers import StrikePoller
        mocker.patch("pollers.feedparser.parse", return_value=self._patched_feed())
        mocker.patch("pollers._fetch_page_body", return_value=self._patched_page())
        mocker.patch("pollers.extract_alert_details", return_value={"not_a_strike": True})

        alerts = StrikePoller(feeds=["https://fake.test/rss"], max_age_days=365).fetch()
        assert len(alerts) == 0
