import pytest

from pulse_categories import (
    CATEGORY_SOURCES,
    STATUS_LEVELS,
    compute_categories,
    count_alerts_by_category,
    determine_status,
    determine_trend,
    get_baseline,
)


def _alert(source, stale=False):
    return {"source": source, "stale": 1 if stale else 0}


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


class TestGetBaseline:
    def _pulse(self, hour, cats):
        return {
            "generated_at": f"2026-06-20T{hour:02d}:00:00Z",
            "categories": cats,
        }

    def test_same_hour_used(self):
        pulses = [
            self._pulse(10, {"transport": {"count": 4}}),
            self._pulse(10, {"transport": {"count": 6}}),
            self._pulse(15, {"transport": {"count": 20}}),
        ]
        baseline = get_baseline(pulses, current_hour=10)
        assert baseline["transport"] == 5.0

    def test_adjacent_hours_included(self):
        pulses = [
            self._pulse(9, {"transport": {"count": 2}}),
            self._pulse(10, {"transport": {"count": 4}}),
            self._pulse(11, {"transport": {"count": 6}}),
        ]
        baseline = get_baseline(pulses, current_hour=10)
        assert baseline["transport"] == 4.0

    def test_missing_count_skipped(self):
        pulses = [
            self._pulse(10, {"transport": {"status": "normal"}}),
            self._pulse(10, {"transport": {"count": 6}}),
        ]
        baseline = get_baseline(pulses, current_hour=10)
        assert baseline["transport"] == 6.0

    def test_empty_returns_empty(self):
        assert get_baseline([], current_hour=10) == {}

    def test_no_matching_hour_returns_empty(self):
        pulses = [self._pulse(15, {"transport": {"count": 10}})]
        assert get_baseline(pulses, current_hour=3) == {}

    def test_midnight_wraps(self):
        pulses = [
            self._pulse(23, {"weather": {"count": 3}}),
            self._pulse(0, {"weather": {"count": 5}}),
        ]
        baseline = get_baseline(pulses, current_hour=0)
        assert baseline["weather"] == 4.0


class TestDetermineStatus:
    def test_zero_alerts(self):
        assert determine_status(0, 5.0) == "clear"
        assert determine_status(0, None) == "clear"

    def test_cold_start(self):
        assert determine_status(3, None) == "low"

    def test_new_source_no_baseline(self):
        assert determine_status(2, 0) == "moderate"

    def test_at_average(self):
        assert determine_status(5, 5.0) == "low"

    def test_slightly_above(self):
        assert determine_status(6, 5.0) == "moderate"

    def test_well_above(self):
        assert determine_status(9, 5.0) == "high"

    def test_below_average(self):
        assert determine_status(2, 5.0) == "low"


class TestDetermineTrend:
    def test_cold_start(self):
        assert determine_trend("low", None) == "stable"

    def test_same_level(self):
        assert determine_trend("low", "low") == "stable"
        assert determine_trend("high", "high") == "stable"

    def test_worsening(self):
        assert determine_trend("moderate", "low") == "worsening"
        assert determine_trend("high", "clear") == "worsening"

    def test_improving(self):
        assert determine_trend("low", "moderate") == "improving"
        assert determine_trend("clear", "high") == "improving"

    def test_invalid_status_returns_stable(self):
        assert determine_trend("unknown", "low") == "stable"


class TestComputeCategories:
    def test_basic(self):
        alerts = [_alert("rmv"), _alert("rmv"), _alert("dwd")]
        result = compute_categories(alerts, previous_pulse=None, history_pulses=[], current_hour=10)

        assert result["transport"] == {"status": "low", "trend": "stable", "count": 2}
        assert result["weather"] == {"status": "low", "trend": "stable", "count": 1}
        assert result["roadworks"] == {"status": "clear", "trend": "stable", "count": 0}

    def test_trend_from_previous(self):
        alerts = [_alert("rmv"), _alert("rmv"), _alert("rmv")]
        prev = {"categories": {"transport": {"status": "clear", "count": 0}}}
        result = compute_categories(alerts, previous_pulse=prev, history_pulses=[], current_hour=10)

        assert result["transport"]["trend"] == "worsening"

    def test_all_categories_present(self):
        result = compute_categories([], previous_pulse=None, history_pulses=[], current_hour=10)
        assert set(result.keys()) == set(CATEGORY_SOURCES.keys())
        for cat in result.values():
            assert cat == {"status": "clear", "trend": "stable", "count": 0}

    def test_with_baseline(self):
        alerts = [_alert("rmv")] * 10
        history = [{
            "generated_at": f"2026-06-{d:02d}T10:00:00Z",
            "categories": {"transport": {"count": 5}},
        } for d in range(15, 22)]

        result = compute_categories(alerts, previous_pulse=None, history_pulses=history, current_hour=10)
        assert result["transport"]["status"] == "high"
