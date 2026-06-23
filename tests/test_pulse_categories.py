from datetime import datetime, timezone

import pytest

from pulse_categories import (
    CATEGORY_SOURCES,
    EWMA_ALPHA,
    STATUS_LEVELS,
    _compute_weight,
    compute_categories,
    compute_ewma,
    compute_travel_ok,
    count_alerts_by_category,
    determine_status,
    determine_trend,
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

    def test_no_valid_from_with_valid_until_future(self):
        alerts = [_alert("rmv", valid_until=_FUTURE)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 1.0

    def test_incidents_always_counted(self):
        alerts = [
            _alert("polizei"),
            _alert("strike", valid_from=_FUTURE),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["incidents"] == 2.0

    def test_no_temporal_fields_non_incident_counted(self):
        alerts = [_alert("rmv")]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == 1.0


class TestSeverityWeighting:
    def test_dwd_severity_weights(self):
        alerts = [
            _alert("dwd", valid_from=_PAST, severity=1),
            _alert("dwd", valid_from=_PAST, severity=4),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["weather"] == pytest.approx(2.5)  # 0.5 + 2.0

    def test_rmv_service_weights(self):
        alerts = [
            _alert("rmv", valid_from=_PAST, service="S-Bahn"),
            _alert("rmv", valid_from=_PAST, service="Bus"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["transport"] == pytest.approx(2.5)  # 1.5 + 1.0

    def test_autobahn_closure_weight(self):
        alerts = [
            _alert("autobahn", valid_from=_PAST, title="A5 Closure near Friedberg"),
            _alert("autobahn", valid_from=_PAST, title="A3 Warning: roadworks"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["roadworks"] == pytest.approx(2.5)  # 1.5 + 1.0

    def test_baustellen_full_closure_weight(self):
        alerts = [
            _alert("baustellen", valid_from=_PAST, service="City (Full)"),
            _alert("baustellen", valid_from=_PAST, service="City (Partial)"),
        ]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["roadworks"] == pytest.approx(2.5)  # 1.5 + 1.0

    def test_events_weight(self):
        alerts = [_alert("events", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["events"] == pytest.approx(2.0)

    def test_sports_weight(self):
        alerts = [_alert("sports", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["events"] == pytest.approx(2.0)

    def test_default_weight_polizei(self):
        alerts = [_alert("polizei")]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["incidents"] == pytest.approx(1.0)

    def test_messe_weight(self):
        alerts = [_alert("messe", valid_from=_PAST)]
        counts = count_alerts_by_category(alerts, now=_NOW)
        assert counts["events"] == pytest.approx(2.0)


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

    def test_autobahn_warning(self):
        assert _compute_weight({"source": "autobahn", "title_en": "A5 Warning"}) == 1.0

    def test_messe(self):
        assert _compute_weight({"source": "messe"}) == 2.0

    def test_unknown_source(self):
        assert _compute_weight({"source": "unknown"}) == 1.0


class TestComputeEwma:
    def _pulse(self, hour, cats, day=20):
        return {
            "generated_at": f"2026-06-{day:02d}T{hour:02d}:00:00Z",
            "categories": cats,
        }

    def test_single_pulse(self):
        pulses = [self._pulse(10, {"transport": {"count": 8}})]
        ewma = compute_ewma(pulses)
        assert ewma["transport"] == 8.0

    def test_two_pulses(self):
        pulses = [
            self._pulse(10, {"transport": {"count": 10}}, day=19),
            self._pulse(11, {"transport": {"count": 10}}, day=19),
        ]
        ewma = compute_ewma(pulses)
        assert ewma["transport"] == 10.0

    def test_ewma_responds_to_spike(self):
        pulses = [
            self._pulse(10, {"transport": {"count": 5}}, day=19),
            self._pulse(11, {"transport": {"count": 5}}, day=19),
            self._pulse(12, {"transport": {"count": 20}}, day=19),
        ]
        ewma = compute_ewma(pulses)
        # After [5, 5]: ewma = 0.3*5 + 0.7*5 = 5.0
        # After 20: ewma = 0.3*20 + 0.7*5 = 9.5
        assert ewma["transport"] == 9.5

    def test_ewma_decays_old_values(self):
        pulses = [
            self._pulse(10, {"transport": {"count": 100}}, day=18),
            self._pulse(11, {"transport": {"count": 5}}, day=18),
            self._pulse(12, {"transport": {"count": 5}}, day=18),
            self._pulse(13, {"transport": {"count": 5}}, day=18),
        ]
        ewma = compute_ewma(pulses)
        # Initial spike of 100 should decay significantly after 3 more data points
        assert ewma["transport"] < 40

    def test_multiple_categories(self):
        pulses = [
            self._pulse(10, {"transport": {"count": 8}, "weather": {"count": 2}}),
        ]
        ewma = compute_ewma(pulses)
        assert ewma["transport"] == 8.0
        assert ewma["weather"] == 2.0

    def test_empty_history(self):
        assert compute_ewma([]) == {}

    def test_missing_count_skipped(self):
        pulses = [
            self._pulse(10, {"transport": {"status": "normal"}}),
            self._pulse(11, {"transport": {"count": 6}}),
        ]
        ewma = compute_ewma(pulses)
        assert ewma["transport"] == 6.0

    def test_chronological_ordering(self):
        # Pulses passed out of order should still be processed chronologically
        pulses = [
            self._pulse(12, {"transport": {"count": 20}}, day=19),
            self._pulse(10, {"transport": {"count": 5}}, day=19),
        ]
        ewma = compute_ewma(pulses)
        # Sorted: 10:00 (count=5) first, then 12:00 (count=20)
        # ewma = 0.3*20 + 0.7*5 = 9.5
        assert ewma["transport"] == 9.5


class TestDetermineStatus:
    def test_zero_alerts(self):
        assert determine_status(0, 5.0) == "clear"
        assert determine_status(0, None) == "clear"

    def test_cold_start(self):
        assert determine_status(3, None) == "moderate"

    def test_new_source_no_baseline(self):
        assert determine_status(2, 0) == "moderate"

    def test_at_ewma(self):
        assert determine_status(5, 5.0) == "low"

    def test_slightly_above(self):
        # 7/5 = 1.4 → moderate (> 1.3)
        assert determine_status(7, 5.0) == "moderate"

    def test_well_above(self):
        # 9/5 = 1.8 → high (> 1.6)
        assert determine_status(9, 5.0) == "high"

    def test_below_ewma(self):
        assert determine_status(2, 5.0) == "low"

    def test_at_1_3_boundary(self):
        # 6.5/5.0 = 1.3 → low (≤ 1.3)
        assert determine_status(65, 50.0) == "low"

    def test_just_above_1_3(self):
        # 6.6/5.0 = 1.32 → moderate
        assert determine_status(66, 50.0) == "moderate"

    def test_at_1_6_boundary(self):
        # 8.0/5.0 = 1.6 → moderate (≤ 1.6)
        assert determine_status(80, 50.0) == "moderate"

    def test_just_above_1_6(self):
        # 8.1/5.0 = 1.62 → high
        assert determine_status(81, 50.0) == "high"


class TestDetermineTrend:
    def test_cold_start(self):
        assert determine_trend(5, None) == "stable"

    def test_zero_ewma(self):
        assert determine_trend(5, 0) == "stable"

    def test_stable_at_ewma(self):
        assert determine_trend(5, 5.0) == "stable"

    def test_stable_slightly_above(self):
        # 6/5 = 1.2 → stable (within ±30%)
        assert determine_trend(6, 5.0) == "stable"

    def test_stable_slightly_below(self):
        # 4/5 = 0.8 → stable (within ±30%)
        assert determine_trend(4, 5.0) == "stable"

    def test_worsening(self):
        # 7/5 = 1.4 → worsening (> 1.3)
        assert determine_trend(7, 5.0) == "worsening"

    def test_improving(self):
        # 3/5 = 0.6 → improving (< 0.7)
        assert determine_trend(3, 5.0) == "improving"

    def test_zero_count_improving(self):
        assert determine_trend(0, 5.0) == "improving"


class TestComputeCategories:
    def test_basic(self):
        alerts = [_alert("rmv"), _alert("rmv"), _alert("dwd")]
        result = compute_categories(alerts, previous_pulse=None, history_pulses=[], current_hour=10)

        assert result["transport"]["status"] == "moderate"
        assert result["transport"]["trend"] == "stable"
        assert result["transport"]["count"] == 2
        assert result["transport"]["ewma"] == 0.0
        assert result["weather"]["count"] == 1
        assert result["roadworks"] == {"status": "clear", "trend": "stable", "count": 0, "ewma": 0.0}

    def test_all_categories_present(self):
        result = compute_categories([], previous_pulse=None, history_pulses=[], current_hour=10)
        assert set(result.keys()) == set(CATEGORY_SOURCES.keys())
        for cat in result.values():
            assert cat == {"status": "clear", "trend": "stable", "count": 0, "ewma": 0.0}

    def test_with_ewma_baseline(self):
        alerts = [_alert("rmv")] * 10
        history = [{
            "generated_at": f"2026-06-{d:02d}T10:00:00Z",
            "categories": {"transport": {"count": 5}},
        } for d in range(15, 22)]

        result = compute_categories(alerts, previous_pulse=None, history_pulses=history, current_hour=10)
        # EWMA of 7 pulses all with count=5 → ewma ≈ 5.0
        # 10/5 = 2.0 → high (> 1.6)
        assert result["transport"]["status"] == "high"
        assert result["transport"]["trend"] == "worsening"

    def test_ewma_included_in_output(self):
        history = [{
            "generated_at": "2026-06-20T10:00:00Z",
            "categories": {"transport": {"count": 8}},
        }]
        alerts = [_alert("rmv")] * 8
        result = compute_categories(alerts, previous_pulse=None, history_pulses=history, current_hour=10)
        assert result["transport"]["ewma"] == 8.0

    def test_previous_pulse_not_used_for_trend(self):
        # previous_pulse is no longer used for trend — EWMA determines trend
        alerts = [_alert("rmv")] * 5
        prev = {"categories": {"transport": {"status": "clear", "count": 0}}}
        history = [{
            "generated_at": "2026-06-20T10:00:00Z",
            "categories": {"transport": {"count": 5}},
        }]
        result = compute_categories(alerts, previous_pulse=prev, history_pulses=history, current_hour=10)
        # count=5, ewma=5.0 → ratio=1.0 → stable (not worsening despite prev being "clear")
        assert result["transport"]["trend"] == "stable"


class TestComputeTravelOk:
    def test_all_clear(self):
        cats = {c: {"status": "clear"} for c in CATEGORY_SOURCES}
        assert compute_travel_ok(cats) is True

    def test_all_low(self):
        cats = {c: {"status": "low"} for c in CATEGORY_SOURCES}
        assert compute_travel_ok(cats) is True

    def test_transport_moderate(self):
        cats = {c: {"status": "low"} for c in CATEGORY_SOURCES}
        cats["transport"] = {"status": "moderate"}
        assert compute_travel_ok(cats) is False

    def test_roadworks_high(self):
        cats = {c: {"status": "low"} for c in CATEGORY_SOURCES}
        cats["roadworks"] = {"status": "high"}
        assert compute_travel_ok(cats) is False

    def test_weather_high_still_ok(self):
        cats = {c: {"status": "low"} for c in CATEGORY_SOURCES}
        cats["weather"] = {"status": "high"}
        assert compute_travel_ok(cats) is True
