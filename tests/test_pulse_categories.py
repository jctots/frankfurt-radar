from datetime import datetime, timedelta, timezone

import pytest

from pulse_categories import (
    BASELINE_LAG_BUCKETS,
    CATEGORY_SOURCES,
    CATEGORY_STATUS_LABELS,
    CATEGORY_WINDOWS,
    WEIGHTS_VERSION,
    _compute_weight,
    _is_ongoing,
    _is_upcoming,
    apply_status_hysteresis,
    build_category_timeseries,
    compute_lead_alert,
    compute_pulse_config_version,
    compute_snapshot,
    compute_status,
    compute_status_floor,
    compute_trend,
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


def _empty_snapshot():
    return {
        "ongoing_count": 0, "ongoing_score": 0.0,
        "projected_count": 0, "projected_score": 0.0,
        "upcoming_near_score": 0.0,
        "scheduled_upcoming_score": 0.0,
    }


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
        assert counts["transport"] == pytest.approx(2.0)  # S-Bahn 1.5 + Bus 0.5

    def test_autobahn_closure_weight(self):
        alerts = [
            _alert("autobahn", valid_from=_PAST, title="A5 Closure near Friedberg"),
            _alert("autobahn", valid_from=_PAST, title="A3 Warning: roadworks"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["roadworks"] == pytest.approx(3.0)  # closure 2.0 + no-closure 1.0

    def test_events_weight(self):
        alerts = [_alert("events", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["events"] == pytest.approx(2.0)

    def test_default_weight_polizei(self):
        alerts = [_alert("polizei")]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["incidents"] == pytest.approx(0.5)


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
        assert _compute_weight({"source": "autobahn", "title_en": "Full CLOSURE of A5"}) == 2.0

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
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 2
        assert snap["transport"]["ongoing_score"] == pytest.approx(2.5)

    def test_upcoming_counted_but_not_projected(self):
        upcoming_time = (_NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=upcoming_time, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 0
        # +4h is beyond next 1h interval — not in projected
        assert snap["transport"]["projected_count"] == 0
        # but within 24h lookahead — counted in the lookahead score
        assert snap["transport"]["scheduled_upcoming_score"] == pytest.approx(1.0)
        # +4h is beyond transport's 3h surge_lead_hours window — not yet "lead"
        assert snap["transport"]["upcoming_near_score"] == pytest.approx(0.0)

    def test_upcoming_beyond_lookahead_excluded(self):
        far_future = (_NOW + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=far_future, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["projected_count"] == 0
        assert snap["transport"]["scheduled_upcoming_score"] == 0.0

    def test_stale_excluded(self):
        alerts = [_alert("rmv", stale=True, valid_from=_PAST)]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 0

    def test_all_categories_present(self):
        snap, _ = compute_snapshot([], now=_NOW)
        assert set(snap.keys()) == set(CATEGORY_SOURCES.keys())
        for cat_data in snap.values():
            assert cat_data == {**_empty_snapshot(), "status_floor": None}

    def test_severity_weighting_in_snapshot(self):
        alerts = [
            _alert("dwd", valid_from=_PAST, severity=4),
            _alert("dwd", valid_from=_PAST, severity=1),
        ]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["weather"]["ongoing_count"] == 2
        assert snap["weather"]["ongoing_score"] == pytest.approx(2.5)

    def test_incidents_always_ongoing(self):
        alerts = [_alert("polizei"), _alert("strike")]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["incidents"]["ongoing_count"] == 2
        assert snap["incidents"]["projected_count"] == 2

    def test_projected_uses_next_interval_only(self):
        expiring_soon = (_NOW + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starting_soon = (_NOW + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starting_later = (_NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        beyond_lookahead = (_NOW + timedelta(hours=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=expiring_soon, service="S-Bahn"),
            _alert("rmv", valid_from=_PAST, valid_until=beyond_lookahead),
            _alert("rmv", valid_from=starting_soon, valid_until="2026-06-25T00:00:00Z"),
            _alert("rmv", valid_from=starting_later, valid_until="2026-06-25T00:00:00Z"),
        ]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 2
        assert snap["transport"]["ongoing_score"] == pytest.approx(2.5)
        # projected uses next 1h only: -1.5 (S-Bahn expiring) +1.0 (starting_soon)
        assert snap["transport"]["projected_count"] == 2
        assert snap["transport"]["projected_score"] == pytest.approx(2.0)
        # lookahead score = pure future starts within the 24h lookahead (both starters)
        assert snap["transport"]["scheduled_upcoming_score"] == pytest.approx(2.0)
        # lead score (surge_lead_hours=3) = only starting_soon (+45min); starting_later (+4h) is outside it
        assert snap["transport"]["upcoming_near_score"] == pytest.approx(1.0)

    def test_projected_floor_at_zero(self):
        expiring_time = (_NOW + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=expiring_time),
        ]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["transport"]["ongoing_count"] == 1
        assert snap["transport"]["projected_count"] == 0
        assert snap["transport"]["projected_score"] == 0.0

    def test_no_lookahead_category(self):
        upcoming_time = (_NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [_alert("polizei", valid_from=upcoming_time)]
        snap, _ = compute_snapshot(alerts, now=_NOW)
        assert snap["incidents"]["ongoing_count"] == 1
        assert snap["incidents"]["projected_count"] == 1

    def test_score_breakdown(self):
        expiring_soon = (_NOW + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starting_soon = (_NOW + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
        starting_later = (_NOW + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        alerts = [
            _alert("rmv", valid_from=_PAST, valid_until=expiring_soon, service="S-Bahn"),
            _alert("rmv", valid_from=_PAST, valid_until=_FUTURE),
            _alert("rmv", valid_from=starting_soon, valid_until="2026-06-25T00:00:00Z"),
            _alert("rmv", valid_from=starting_later, valid_until="2026-06-25T00:00:00Z"),
        ]
        _, breakdown = compute_snapshot(alerts, now=_NOW)
        t = breakdown["transport"]
        assert len(t["ongoing"]) == 2
        assert len(t["expiring_near"]) == 1
        assert t["expiring_near"][0]["weight"] == 1.5
        assert len(t["starting_near"]) == 1
        assert t["starting_near"][0]["weight"] == 1.0
        # starting_later (+4h) is within surge_lead_hours=3? no — 4h > 3h, so
        # only starting_soon (+45min) lands in starting_lead too.
        assert len(t["starting_lead"]) == 1
        assert t["starting_lead"][0]["weight"] == 1.0
        assert len(t["starting_full"]) == 2
        for entry in t["ongoing"]:
            assert "alert_id" in entry
            assert "source" in entry
            assert "weight" in entry


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
                "upcoming_near_score": 0.5,
                "scheduled_upcoming_score": 2.0,
            })
        rows.sort(key=lambda r: r["timestamp"])
        return rows

    def test_transport_hourly_history(self):
        snapshots = {"transport": self._make_snapshots("transport", 24)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: {"ongoing_count": 3, "ongoing_score": 5.0,
                         "projected_count": 1, "projected_score": 2.0,
                         "upcoming_near_score": 0.5, "scheduled_upcoming_score": 2.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert "transport" in ts
        assert ts["transport"]["window"] == "24h hourly"
        assert len(ts["transport"]["history"]) <= 24
        assert "hour" in ts["transport"]["history"][0]
        assert "count" in ts["transport"]["history"][0]
        assert "score" in ts["transport"]["history"][0]
        assert "lookahead_score" in ts["transport"]["history"][0]
        assert "lead_score" in ts["transport"]["history"][0]
        assert "projected_score" not in ts["transport"]["history"][0]

    def test_weather_6hourly_aggregation(self):
        snapshots = {"weather": self._make_snapshots("weather", 72)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert ts["weather"]["window"] == "72h 6h"
        assert len(ts["weather"]["history"]) <= 13
        assert "period" in ts["weather"]["history"][0]

    def test_roadworks_daily_aggregation(self):
        snapshots = {"roadworks": self._make_snapshots("roadworks", 168)}

        def get_fn(cat, since):
            return snapshots.get(cat, [])

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)

        assert "4w" in ts["roadworks"]["window"]
        assert "date" in ts["roadworks"]["history"][0]

    def test_all_categories_present(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert set(ts.keys()) == set(CATEGORY_SOURCES.keys())

    def test_current_snapshot_in_output(self):
        def get_fn(cat, since):
            return []

        current = {
            "transport": {"ongoing_count": 5, "ongoing_score": 8.5,
                          "projected_count": 2, "projected_score": 3.0,
                          "upcoming_near_score": 1.5, "scheduled_upcoming_score": 4.5},
        }
        for cat in CATEGORY_SOURCES:
            if cat not in current:
                current[cat] = _empty_snapshot()

        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["current"]["ongoing"]["count"] == 5
        assert ts["transport"]["current"]["ongoing"]["score"] == 8.5
        assert ts["transport"]["current"]["projected"]["count"] == 2
        assert ts["transport"]["current"]["lookahead"]["total_score"] == 4.5
        assert ts["transport"]["current"]["lookahead"]["lead_score"] == 1.5

    def test_empty_history(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["history"] == []


class TestLookaheadInTimeseries:
    def test_incidents_has_no_lookahead(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert "lookahead" not in ts["incidents"]["current"]

    def test_transport_has_lookahead(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert "lookahead" in ts["transport"]["current"]
        assert "total_score" in ts["transport"]["current"]["lookahead"]
        assert "lead_score" in ts["transport"]["current"]["lookahead"]

    def test_lookahead_score_in_history(self):
        rows = []
        for i in range(6):
            ts = (_NOW - timedelta(hours=5 - i)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({
                "timestamp": ts, "category": "transport",
                "ongoing_count": 3, "ongoing_score": 5.0,
                "projected_count": 1, "projected_score": 2.0,
                "upcoming_near_score": 1.0,
                "scheduled_upcoming_score": 1.0 + i,
            })

        def get_fn(cat, since):
            return rows if cat == "transport" else []

        current = {cat: {**_empty_snapshot(), "upcoming_near_score": 3.0, "scheduled_upcoming_score": 6.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        lookahead = ts["transport"]["current"]["lookahead"]
        assert lookahead["total_score"] == 6.0
        assert lookahead["lead_score"] == 3.0
        history = ts["transport"]["history"]
        assert len(history) == 6
        assert history[0]["lookahead_score"] == 1.0
        assert history[-1]["lookahead_score"] > history[0]["lookahead_score"]
        assert history[0]["lead_score"] == 1.0


class TestLeadBaseline:
    """Regression coverage for the lead-baseline design: the lead-alert
    signal used to reuse interval_hours as its "imminent" window, which
    collapsed the warning window into the onset itself for fixed-clock-time
    disruptions (observed 2026-07-05, S8/S9 nighttime cancellation). It also
    used to compare against the ongoing-score baseline, which for
    high-baseload categories (transport, roadworks) made a single new
    alert's schedule weight structurally unable to clear 1.5x mean. The
    lead score now has its own dedicated surge_lead_hours window, with a
    baseline built from its own history, lag-excluded by surge_lead_hours
    (not the full lookahead) so a disruption visible for its whole lead
    window can't absorb itself into its own baseline.
    """

    def test_lag_excludes_recent_self_absorption(self):
        from pulse_categories import _build_lead_baseline

        rows = []
        for h in range(10, 0, -1):  # oldest first
            if h <= 2:
                score = 4.5  # a disruption entered the lead window 2h ago and is still there
            elif h in (5, 7, 9):
                score = 3.0  # genuine older, pre-disruption lead activity
            else:
                score = 0.0
            ts = (_NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({"timestamp": ts, "ongoing_count": 0, "ongoing_score": 0.0,
                         "upcoming_near_score": score})

        baseline = _build_lead_baseline(rows, interval_hours=1, surge_lead_hours=3)
        # lag = ceil(3/1) = 3 buckets excluded from the tail, so the
        # recently-elevated (h<=2) buckets can't reach the baseline.
        assert baseline is not None
        assert baseline["mean"] == pytest.approx(3.0)
        assert baseline["n"] == 3

    def test_lead_alert_fires_against_own_baseline_despite_huge_ongoing_load(self):
        # Transport-shaped data: ongoing load is huge (~120, many chronic
        # disruptions) but the newly-scheduled alert (6.0) is large relative
        # to its OWN quiet lead-window history. Comparing against the
        # ongoing baseline could never fire; against the lead baseline it
        # should.
        rows = []
        for h in range(10, 0, -1):
            score = 1.0 if h in (5, 7, 9) else 0.0
            ts = (_NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({
                "timestamp": ts, "ongoing_count": 30, "ongoing_score": 120.0,
                "upcoming_near_score": score,
            })

        def get_fn(cat, since):
            return rows if cat == "transport" else []

        current = {cat: {"ongoing_count": 30, "ongoing_score": 120.0,
                         "projected_count": 30, "projected_score": 120.0,
                         "upcoming_near_score": 6.0, "scheduled_upcoming_score": 24.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["current"]["lead_alert"] is True

    def test_s8_s9_lead_alert_fires_hours_before_onset(self):
        """Replays the 2026-07-05 S8/S9 nighttime-cancellation scenario: the
        alert's own weight (6.0) enters transport's 3h lead window well
        before its 21:30 valid_from. Quiet lead-window history (no prior
        lead activity) means no baseline yet, so the no-baseline absolute
        floor (>= 2.0) applies — and 6.0 clears it, firing a lead alert
        (and therefore a `worsening` trend) hours before the disruption
        starts, instead of only at the moment status jumps to severe.
        """
        rows = []
        for h in range(10, 0, -1):
            ts = (_NOW - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({
                "timestamp": ts, "ongoing_count": 32, "ongoing_score": 109.0,
                "upcoming_near_score": 0.0,
            })

        def get_fn(cat, since):
            return rows if cat == "transport" else []

        current = {cat: {"ongoing_count": 32, "ongoing_score": 109.0,
                         "projected_count": 32, "projected_score": 109.0,
                         "upcoming_near_score": 6.0, "scheduled_upcoming_score": 18.0}
                   for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        assert ts["transport"]["current"]["lead_alert"] is True
        assert ts["transport"]["current"]["trend"] == "worsening"


class TestComputeStatus:
    _BASELINE = {"mean": 10.0, "p25": 5.0, "p75": 20.0, "n": 24}

    def test_zero_is_clear(self):
        assert compute_status(0.0, self._BASELINE) == "clear"

    def test_no_baseline_is_minor(self):
        assert compute_status(50.0, None) == "minor"

    def test_thresholds(self):
        assert compute_status(8.0, self._BASELINE) == "minor"
        assert compute_status(15.0, self._BASELINE) == "moderate"
        assert compute_status(25.0, self._BASELINE) == "severe"

    def test_skewed_baseline_moderate_band_not_empty(self):
        # nonzero-only stats can put mean above p75 — min/max guard applies
        skewed = {"mean": 49.5, "p25": 30.0, "p75": 44.0, "n": 24}
        assert compute_status(40.0, skewed) == "minor"      # <= min(mean, p75)
        assert compute_status(46.0, skewed) == "moderate"    # between
        assert compute_status(55.0, skewed) == "severe"      # > max(mean, p75)

    def test_flat_baseline_still_has_moderate_band(self):
        # mean ~= p75 (near-zero natural variance, e.g. transport 2026-07-05:
        # mean=109.86, p75=109.0) — the raw band is <1 point wide; the
        # minimum-band-width floor must still leave room for "moderate".
        flat = {"mean": 109.86, "p25": 109.0, "p75": 109.0, "n": 21}
        min_hi = 109.0 + 109.86 * 0.1  # lo + mean * MIN_MODERATE_BAND_FRACTION
        assert compute_status(109.0, flat) == "minor"
        assert compute_status(min_hi - 0.01, flat) == "moderate"
        assert compute_status(min_hi + 0.01, flat) == "severe"

    def test_floor_raises_status(self):
        assert compute_status(1.0, None, floor="moderate") == "moderate"
        assert compute_status(1.0, self._BASELINE, floor="severe") == "severe"

    def test_floor_does_not_lower_status(self):
        assert compute_status(25.0, self._BASELINE, floor="moderate") == "severe"

    def test_floor_ignored_when_clear(self):
        assert compute_status(0.0, self._BASELINE, floor="severe") == "clear"


class TestComputeStatusFloor:
    def test_dwd_extreme_floors_severe(self):
        alerts = [{"source": "dwd", "severity": 4}]
        assert compute_status_floor("weather", alerts) == "severe"

    def test_dwd_severe_floors_moderate(self):
        alerts = [{"source": "dwd", "severity": 3}, {"source": "dwd", "severity": 1}]
        assert compute_status_floor("weather", alerts) == "moderate"

    def test_dwd_minor_no_floor(self):
        assert compute_status_floor("weather", [{"source": "dwd", "severity": 2}]) is None

    def test_no_alerts_no_floor(self):
        assert compute_status_floor("weather", []) is None

    def test_other_categories_no_floor(self):
        assert compute_status_floor("transport", [{"source": "rmv", "severity": 4}]) is None


class TestStatusHysteresis:
    def test_first_run_takes_raw(self):
        assert apply_status_hysteresis("moderate", None, 0, True) == ("moderate", 0)

    def test_escalation_immediate(self):
        assert apply_status_hysteresis("severe", "minor", 0, True) == ("severe", 0)

    def test_same_status_resets_pending(self):
        assert apply_status_hysteresis("moderate", "moderate", 1, True) == ("moderate", 0)

    def test_deescalation_needs_two_confirmations(self):
        eff, pending = apply_status_hysteresis("moderate", "severe", 0, True)
        assert (eff, pending) == ("severe", 1)
        eff, pending = apply_status_hysteresis("moderate", "severe", pending, True)
        assert (eff, pending) == ("moderate", 0)

    def test_flapping_damped(self):
        # July 1 pattern: raw sev->mod->sev->mod — effective should stay severe
        eff, p = apply_status_hysteresis("moderate", "severe", 0, True)
        assert eff == "severe"
        eff, p = apply_status_hysteresis("severe", eff, p, True)
        assert (eff, p) == ("severe", 0)
        eff, p = apply_status_hysteresis("moderate", eff, p, True)
        assert eff == "severe"

    def test_no_advance_no_pending_consumption(self):
        eff, pending = apply_status_hysteresis("clear", "moderate", 1, False)
        assert (eff, pending) == ("moderate", 1)


class TestComputeLeadAlert:
    _BASELINE = {"mean": 10.0, "p25": 5.0, "p75": 20.0, "n": 24}

    def test_no_lead_score_no_alert(self):
        assert compute_lead_alert(0.0, self._BASELINE) is False

    def test_large_vs_own_baseline_alerts(self):
        assert compute_lead_alert(20.0, self._BASELINE) is True

    def test_small_vs_own_baseline_no_alert(self):
        assert compute_lead_alert(5.0, self._BASELINE) is False

    def test_no_baseline_requires_absolute_floor(self):
        assert compute_lead_alert(2.5, None) is True
        assert compute_lead_alert(1.0, None) is False


class TestComputeTrend:
    def _history(self, scores):
        return [{"hour": f"h{i}", "count": 1, "score": s, "lookahead_score": 0.0}
                for i, s in enumerate(scores)]

    def test_empty_history_stable(self):
        assert compute_trend(5.0, [], False) == "stable"

    def test_flat_is_stable(self):
        h = self._history([10.0, 10.0, 10.0, 10.0])
        assert compute_trend(10.0, h, False) == "stable"

    def test_small_noise_is_stable(self):
        # +-4% flips used to flip the LLM's judgment — dead band absorbs them
        h = self._history([131.0, 137.0, 131.0, 137.0])
        assert compute_trend(137.0, h, False) == "stable"

    def test_rising_is_worsening(self):
        h = self._history([10.0, 10.0, 10.0, 35.0])
        assert compute_trend(35.0, h, False) == "worsening"

    def test_falling_is_improving(self):
        h = self._history([30.0, 30.0, 30.0, 10.0])
        assert compute_trend(10.0, h, False) == "improving"

    def test_lead_alert_escalates_one_step(self):
        h = self._history([10.0, 10.0, 10.0, 10.0])
        assert compute_trend(10.0, h, True) == "worsening"
        falling = self._history([30.0, 30.0, 30.0, 10.0])
        assert compute_trend(10.0, falling, True) == "stable"

    def test_tiny_scores_use_absolute_band(self):
        # ref 1.0 with 15% band would flip on 0.2 — absolute band of 1.0 absorbs it
        h = self._history([1.0, 1.0, 1.0, 1.2])
        assert compute_trend(1.2, h, False) == "stable"


class TestLaggedBaseline:
    def test_recent_buckets_excluded_from_baseline(self):
        # 24 hourly rows: 20 quiet (5.0) then 4 elevated (100.0, the "event").
        # With the 3-bucket lag, the event's last 3 hours must not enter the
        # baseline — it stays anchored near the quiet level.
        rows = []
        for i in range(24):
            ts = (_NOW - timedelta(hours=23 - i)).strftime("%Y-%m-%dT%H:00:00Z")
            score = 100.0 if i >= 20 else 5.0
            rows.append({
                "timestamp": ts, "category": "transport",
                "ongoing_count": 3, "ongoing_score": score,
                "projected_count": 0, "projected_score": 0.0,
                "upcoming_near_score": 0.0, "scheduled_upcoming_score": 0.0,
            })

        def get_fn(cat, since):
            return rows if cat == "transport" else []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        current["transport"]["ongoing_score"] = 100.0
        current["transport"]["ongoing_count"] = 30

        ts = build_category_timeseries(get_fn, current, now=_NOW)
        baseline = ts["transport"]["baseline"]
        # pool = 21 rows (24 - 3 lag) = 20 quiet + 1 event hour
        assert baseline["n"] == 21
        assert baseline["p75"] == 5.0
        # so the ongoing event still reads severe instead of normalizing away
        assert ts["transport"]["current"]["status"] == "severe"

    def test_short_history_no_baseline(self):
        rows = []
        for i in range(4):
            ts = (_NOW - timedelta(hours=3 - i)).strftime("%Y-%m-%dT%H:00:00Z")
            rows.append({
                "timestamp": ts, "category": "transport",
                "ongoing_count": 1, "ongoing_score": 5.0,
                "projected_count": 0, "projected_score": 0.0,
                "upcoming_near_score": 0.0, "scheduled_upcoming_score": 0.0,
            })

        def get_fn(cat, since):
            return rows if cat == "transport" else []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        # only 1 pool row after lag — not enough for a baseline
        assert ts["transport"]["baseline"] is None


class TestTimeseriesDeterministicFields:
    def test_trend_and_lead_alert_present(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        for cat in CATEGORY_SOURCES:
            assert ts[cat]["current"]["trend"] in ("improving", "stable", "worsening")
            assert ts[cat]["current"]["lead_alert"] in (True, False)

    def test_status_floor_applied_in_timeseries(self):
        def get_fn(cat, since):
            return []

        current = {cat: _empty_snapshot() for cat in CATEGORY_SOURCES}
        current["weather"] = {**current["weather"], "ongoing_count": 1,
                              "ongoing_score": 2.0, "status_floor": "severe"}
        ts = build_category_timeseries(get_fn, current, now=_NOW)
        # no baseline → would be "minor"; DWD floor forces severe
        assert ts["weather"]["current"]["status"] == "severe"


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
            assert "surge_lead_hours" in w

    def test_surge_lead_hours_shorter_than_lookahead(self):
        # surge_lead_hours is meant as a narrower "is this soon" window than
        # the full lookahead — it should never be wider.
        for cat, w in CATEGORY_WINDOWS.items():
            if w["lookahead_hours"] > 0:
                assert 0 < w["surge_lead_hours"] <= w["lookahead_hours"]
            else:
                assert w["surge_lead_hours"] == 0


class TestPulseConfigVersion:
    def test_stable_for_identical_inputs(self):
        assert compute_pulse_config_version("template a") == compute_pulse_config_version("template a")

    def test_changes_when_prompt_changes(self):
        assert compute_pulse_config_version("template a") != compute_pulse_config_version("template b")

    def test_changes_when_weights_version_changes(self, monkeypatch):
        import pulse_categories

        before = compute_pulse_config_version("template a")
        monkeypatch.setattr(pulse_categories, "WEIGHTS_VERSION", pulse_categories.WEIGHTS_VERSION + 1)
        after = pulse_categories.compute_pulse_config_version("template a")
        assert before != after

    def test_changes_when_window_config_changes(self, monkeypatch):
        import pulse_categories

        before = compute_pulse_config_version("template a")
        patched = {**CATEGORY_WINDOWS, "transport": {**CATEGORY_WINDOWS["transport"], "interval_hours": 99}}
        monkeypatch.setattr(pulse_categories, "CATEGORY_WINDOWS", patched)
        after = pulse_categories.compute_pulse_config_version("template a")
        assert before != after

    def test_returns_short_hex_string(self):
        version = compute_pulse_config_version("template a")
        assert len(version) == 8
        int(version, 16)  # raises if not valid hex
