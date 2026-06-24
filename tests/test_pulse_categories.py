from datetime import datetime, timedelta, timezone

import pytest

from pulse_categories import (
    CATEGORY_SOURCES,
    CATEGORY_STATUS_LABELS,
    CATEGORY_WINDOWS,
    _compute_weight,
    _is_ongoing,
    _is_upcoming,
    build_category_timeseries,
    compute_snapshot,
    count_alerts_by_category,
)


def _alert(source, stale=False, valid_from=None, valid_until=None,
           severity=None, service=None, title=None):
    d = {"source": source, "stale": 1 if stale else 0}
    if valid_from is not None:
        d["valid_from"] = valid_from
    if valid_until is not None:
        d["valid_until"] = valid_until
    if severity is not None:
        d["severity"] = severity
    if service is not None:
        d["service"] = service
    if title is not None:
        d["title_en"] = title
    return d


_NOW = datetime(2026, 6, 23, 12, 0, 0, tzinfo=timezone.utc)
_NOW_ISO = "2026-06-23T12:00:00Z"
_PAST = "2026-06-23T10:00:00Z"
_FUTURE = "2026-06-24T10:00:00Z"


class TestCountAlertsByCategory:
    def test_basic_grouping(self):
        alerts = [_alert("rmv"), _alert("rmv"), _alert("dwd"), _alert("autobahn")]
        counts = count_alerts_by_category(alerts)
        assert counts == {"weather": 1, "transport": 2, "roadworks": 1, "incidents": 0, "events": 0}

    def test_excludes_stale(self):
        alerts = [_alert("rmv"), _alert("rmv", stale=True), _alert("dwd")]
        counts = count_alerts_by_category(alerts)
        assert counts["transport"] == 1
        assert counts["weather"] == 1

    def test_multiple_sources_per_category(self):
        alerts = [_alert("autobahn"), _alert("baustellen"), _alert("polizei"), _alert("strike")]
        counts = count_alerts_by_category(alerts)
        assert counts["roadworks"] == 2
        assert counts["incidents"] == 2

    def test_empty_alerts(self):
        counts = count_alerts_by_category([])
        assert all(v == 0 for v in counts.values())

    def test_unknown_source_ignored(self):
        alerts = [_alert("unknown"), _alert("rmv")]
        counts = count_alerts_by_category(alerts)
        assert counts["transport"] == 1
        assert sum(counts.values()) == 1


class TestTemporalFiltering:
    def test_future_alert_excluded(self):
        alerts = [_alert("rmv", valid_from=_FUTURE, valid_until="2026-06-25T10:00:00Z")]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 0.0

    def test_ongoing_alert_counted(self):
        alerts = [_alert("rmv", valid_from=_PAST, valid_until=_FUTURE)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 1.0

    def test_expired_alert_excluded(self):
        alerts = [_alert("rmv", valid_from="2026-06-22T10:00:00Z", valid_until=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 0.0

    def test_open_ended_alert_counted(self):
        alerts = [_alert("rmv", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 1.0

    def test_incidents_always_counted(self):
        alerts = [
            _alert("polizei"),
            _alert("strike", valid_from=_FUTURE),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["incidents"] == 2.0


class TestSeverityWeighting:
    def test_dwd_severity_weights(self):
        alerts = [
            _alert("dwd", valid_from=_PAST, severity=1),
            _alert("dwd", valid_from=_PAST, severity=4),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["weather"] == pytest.approx(2.5)

    def test_rmv_service_weights(self):
        alerts = [
            _alert("rmv", valid_from=_PAST, service="S-Bahn"),
            _alert("rmv", valid_from=_PAST, service="Bus"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == pytest.approx(2.5)

    def test_autobahn_closure_weight(self):
        alerts = [
            _alert("autobahn", valid_from=_PAST, title="A5 Closure near Friedberg"),
            _alert("autobahn", valid_from=_PAST, title="A3 Warning: roadworks"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["roadworks"] == pytest.approx(2.5)

    def test_events_weight(self):
        alerts = [_alert("events", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["events"] == pytest.approx(2.0)

    def test_default_weight_polizei(self):
        alerts = [_alert("polizei")]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["incidents"] == pytest.approx(1.0)


class TestComputeWeight:
    def test_dwd_all_levels(self):
        for sev, expected in [(1, 0.5), (2, 1.0), (3, 1.5), (4, 2.0)]:
            assert _compute_weight({"source": "dwd", "severity": sev}) == expected

    def test_dwd_missing_severity(self):
        assert _compute_weight({"source": "dwd"}) == 1.0

    def test_rmv_ubahn(self):
        assert _compute_weight({"source": "rmv", "service": "U-Bahn"}) == 1.5

    def test_rmv_tram(self):
        assert _compute_weight({"source": "rmv", "service": "Tram"}) == 1.0

    def test_autobahn_closure_case_insensitive(self):
        assert _compute_weight({"source": "autobahn", "title_en": "Full CLOSURE of A5"}) == 1.5

    def test_messe(self):
        assert _compute_weight({"source": "messe"}) == 2.0

    def test_unknown_source(self):
        assert _compute_weight({"source": "unknown"}) == 1.0


class TestComputeSnapshot:
    def test_ongoing_counted(self):
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=_FUTURE),
            _alert("rmv", valid_from=_PAST, valid_until=_FUTURE, service="S-Bahn"),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 2
        assert snap["transport"]["ongoing_score"] == pytest.approx(2.5)

    def test_projected_counted(self):
        upcoming_time = (_NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=upcoming_time, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["projected_count"] == 1
        assert snap["transport"]["ongoing_count"] == 0

    def test_upcoming_beyond_lookahead_excluded(self):
        far_future = (_NOW + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=far_future, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["projected_count"] == 0

    def test_stale_excluded(self):
        alerts = [_alert("rmv", stale=True, valid_from=_PAST)]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 0

    def test_all_categories_present(self):
        snap = compute_snapshot([], now=_NOW)
        assert set(snap.keys()) == set(CATEGORY_SOURCES.keys())
        for cat_data in snap.values():
            assert cat_data == {
                "ongoing_count": 0, "ongoing_score": 0.0,
                "projected_count": 0, "projected_score": 0.0,
            }

    def test_severity_weighting_in_snapshot(self):
        alerts = [
            _alert("dwd", valid_from=_PAST, severity=4),
            _alert("dwd", valid_from=_PAST, severity=1),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["weather"]["ongoing_count"] == 2
        assert snap["weather"]["ongoing_score"] == pytest.approx(2.5)

    def test_incidents_always_ongoing(self):
        alerts = [_alert("polizei"), _alert("strike")]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["incidents"]["ongoing_count"] == 2
        assert snap["incidents"]["projected_count"] == 2

    def test_projected_net_calculation(self):
        expiring_time = (_NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starting_time = (_NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=expiring_time, service="S-Bahn"),
            _alert("rmv", valid_from=_PAST, valid_until=_FUTURE),
            _alert("rmv", valid_from=starting_time, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 2
        assert snap["transport"]["ongoing_score"] == pytest.approx(2.5)
        assert snap["transport"]["projected_count"] == 2
        assert snap["transport"]["projected_score"] == pytest.approx(2.0)

    def test_projected_floor_at_zero(self):
        expiring_time = (_NOW + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=expiring_time),
        ]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 1
        assert snap["transport"]["projected_count"] == 0
        assert snap["transport"]["projected_score"] == 0.0

    def test_no_lookahead_category(self):
        upcoming_time = (_NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [_alert("polizei", valid_from=upcoming_time)]
        snap = compute_snapshot(alerts, now=_NOW)
        assert snap["incidents"]["ongoing_count"] == 1
        assert snap["incidents"]["projected_count"] == 1


class TestIsUpcoming:
    def test_within_lookahead(self):
        future_3h = (_NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (_NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_upcoming({"source": "rmv", "valid_from": future_3h}, _NOW_ISO, end_iso) is True

    def test_beyond_lookahead(self):
        future_12h = (_NOW + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (_NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_upcoming({"source": "rmv", "valid_from": future_12h}, _NOW_ISO, end_iso) is False

    def test_ongoing_not_upcoming(self):
        end_iso = (_NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_upcoming({"source": "rmv", "valid_from": _PAST}, _NOW_ISO, end_iso) is False

    def test_no_temporal_sources_never_upcoming(self):
        future_3h = (_NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = (_NOW + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _is_upcoming({"source": "polizei", "valid_from": future_3h}, _NOW_ISO, end_iso) is False


class TestBuildCategoryTimeseries:
    def _make_snapshots(self, category, hours_back, score=5.0):
        rows = []
        for h in range(hours_back):
            ts = (_NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({
                "timestamp": ts, "category": category,
                "ongoing_count": 3, "ongoing_score": score,
                "projected_count": 1, "projected_score": 2.0,
            })
        rows.sort(key=lambda r: r["timestamp"])
        return rows

    def test_transport_hourly_history(self):
        snapshots = {"transport": self._make_snapshots("transport", 24)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: {"ongoing_count": 3, "ongoing_score": 5.0,
                         "projected_count": 1, "projected_score": 2.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert "transport" in ts
        assert ts["transport"]["window"] == "24h hourly"
        assert len(ts["transport"]["history"]) <= 24
        assert "hour" in ts["transport"]["history"][0]
        assert "count" in ts["transport"]["history"][0]
        assert "score" in ts["transport"]["history"][0]
        assert "projected_score" not in ts["transport"]["history"][0]

    def test_weather_6hourly_aggregation(self):
        snapshots = {"weather": self._make_snapshots("weather", 72)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: {"ongoing_count": 0, "ongoing_score": 0.0,
                         "projected_count": 0, "projected_score": 0.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert ts["weather"]["window"] == "72h 6h"
        assert len(ts["weather"]["history"]) <= 13
        assert "period" in ts["weather"]["history"][0]

    def test_roadworks_daily_aggregation(self):
        snapshots = {"roadworks": self._make_snapshots("roadworks", 168)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: {"ongoing_count": 0, "ongoing_score": 0.0,
                         "projected_count": 0, "projected_score": 0.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert "4w" in ts["roadworks"]["window"]
        assert "date" in ts["roadworks"]["history"][0]

    def test_all_categories_present(self):
        def get_fn(cat, since):
            return []

        current = {cat: {"ongoing_count": 0, "ongoing_score": 0.0,
                         "projected_count": 0, "projected_score": 0.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert set(ts.keys()) == set(CATEGORY_SOURCES.keys())

    def test_current_snapshot_in_output(self):
        def get_fn(cat, since):
            return []

        current = {
            "transport": {"ongoing_count": 5, "ongoing_score": 8.5,
                          "projected_count": 2, "projected_score": 3.0},
        }
        for cat in CATEGORY_SOURCES:
            if cat not in current:
                current[cat] = {"ongoing_count": 0, "ongoing_score": 0.0,
                                "projected_count": 0, "projected_score": 0.0}

        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["current"]["ongoing"]["count"] == 5
        assert ts["transport"]["current"]["ongoing"]["score"] == 8.5
        assert ts["transport"]["current"]["projected"]["count"] == 2

    def test_empty_history(self):
        def get_fn(cat, since):
            return []

        current = {cat: {"ongoing_count": 0, "ongoing_score": 0.0,
                         "projected_count": 0, "projected_score": 0.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["history"] == []


class TestCategoryConfig:
    def test_all_categories_have_status_labels(self):
        for cat in CATEGORY_SOURCES:
            assert cat in CATEGORY_STATUS_LABELS
            assert len(CATEGORY_STATUS_LABELS[cat]) == 4
            assert CATEGORY_STATUS_LABELS[cat][0] == "clear"

    def test_all_categories_have_windows(self):
        for cat in CATEGORY_SOURCES:
            assert cat in CATEGORY_WINDOWS
            w = CATEGORY_WINDOWS[cat]
            assert "interval_hours" in w
            assert "history_hours" in w
            assert "lookahead_hours" in w
