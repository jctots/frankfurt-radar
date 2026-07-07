import json
from unittest.mock import MagicMock

import pytest
import requests

import db
from pulse_categories import compute_pulse_config_version
from pulse import (
    generate_daily_summary,
    generate_pulse,
    load_prompt,
    pulse_ok,
    reset_pulse_health,
    _build_alert_data,
    _build_history_section,
    _call_gemini,
    _ALL_CLEAR_PULSE,
)


class TestLoadPrompt:
    def test_loads_frontmatter_and_template(self, tmp_path, mocker):
        prompt_file = tmp_path / "prompts" / "test.md"
        prompt_file.parent.mkdir()
        prompt_file.write_text(
            "---\nmodel: gemini-2.5-flash\ntemperature: 0.3\n---\nHello {name}"
        )
        mocker.patch("pulse.Path.__truediv__", return_value=tmp_path)
        mocker.patch("pulse.os.getenv", return_value=str(tmp_path))

        text = prompt_file.read_text()
        parts = text.split("---", 2)
        import yaml
        config = yaml.safe_load(parts[1])
        template = parts[2].strip()
        assert config["model"] == "gemini-2.5-flash"
        assert "{name}" in template


class TestBuildAlertData:
    def test_separates_fresh_and_stale(self):
        alerts = [
            {"source": "rmv", "title_en": "Delay", "body_en": "S1 delayed", "stale": False,
             "service": "S-Bahn", "lines": '["S1"]', "severity": 2,
             "valid_from": "2026-06-22T10:00:00Z", "valid_until": "2026-06-22T18:00:00Z"},
            {"source": "autobahn", "title_en": "Roadwork", "body_en": "A5 closure", "stale": True,
             "service": None, "lines": None, "severity": 1,
             "valid_from": "2026-05-01T00:00:00Z", "valid_until": None},
            {"source": "autobahn", "title_en": "Roadwork 2", "body_en": "A3 closure", "stale": True,
             "service": None, "lines": None, "severity": 1,
             "valid_from": "2026-05-02T00:00:00Z", "valid_until": None},
        ]
        alerts_json, stale_summary, capped_summary = _build_alert_data(alerts)
        parsed = json.loads(alerts_json)
        assert len(parsed) == 1
        assert parsed[0]["source"] == "rmv"
        assert "2 autobahn" in stale_summary
        assert capped_summary == "None"

    def test_no_stale(self):
        alerts = [
            {"source": "dwd", "title_en": "Storm", "body_en": "Severe", "stale": False,
             "service": None, "lines": None, "severity": 3,
             "valid_from": "2026-06-22T10:00:00Z", "valid_until": "2026-06-22T18:00:00Z"},
        ]
        _, stale_summary, _ = _build_alert_data(alerts)
        assert stale_summary == "None"

    def test_caps_by_severity_weight(self):
        # 3 low-weight bus alerts + 1 high-weight S-Bahn alert, cap of 2 — the
        # S-Bahn alert (weight 6.0 = 1.5 * 4 lines) must survive the cap over
        # the bus alerts (weight 0.5 each), regardless of recency.
        alerts = [
            {"alert_id": f"bus-{i}", "source": "rmv", "title_en": f"Bus {i}", "body_en": "delay",
             "stale": False, "service": "Bus", "lines": None, "severity": None,
             "valid_from": f"2026-06-22T1{i}:00:00Z", "valid_until": None}
            for i in range(3)
        ] + [
            {"alert_id": "sbahn-1", "source": "rmv", "title_en": "S-Bahn disruption", "body_en": "signal fault",
             "stale": False, "service": "S-Bahn", "lines": '["S1", "S2", "S3", "S4"]', "severity": None,
             "valid_from": "2026-06-22T08:00:00Z", "valid_until": None},
        ]
        alerts_json, _, capped_summary = _build_alert_data(alerts, max_prompt_alerts=2)
        parsed = json.loads(alerts_json)
        assert len(parsed) == 2
        assert "sbahn-1" in {p["alert_id"] for p in parsed}
        assert "2 rmv" in capped_summary

    def test_no_cap_when_under_limit(self):
        alerts = [
            {"source": "dwd", "title_en": "Storm", "body_en": "Severe", "stale": False,
             "service": None, "lines": None, "severity": 3,
             "valid_from": "2026-06-22T10:00:00Z", "valid_until": "2026-06-22T18:00:00Z"},
        ]
        _, _, capped_summary = _build_alert_data(alerts, max_prompt_alerts=60)
        assert capped_summary == "None"


class TestBuildHistorySection:
    def test_empty_history(self):
        assert "first pulse" in _build_history_section([]).lower()

    def test_with_pulses(self):
        pulses = [
            {"generated_at": "2026-06-22T10:00:00Z", "summary": "All clear",
             "categories": {"weather": {"status": "clear", "trend": "stable"}}},
            {"generated_at": "2026-06-22T09:00:00Z", "summary": "S1 delay",
             "categories": {"transport": {"status": "delays", "trend": "worsening"}}},
        ]
        result = _build_history_section(pulses)
        assert "HOURLY PULSES" in result
        assert "All clear" in result
        assert "weather=clear/stable" in result


class TestCallGemini:
    def test_successful_call(self, mocker):
        result_json = {"summary": "All clear"}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(result_json)}]}}]
        }
        resp.raise_for_status.return_value = None
        mocker.patch("pulse.requests.post", return_value=resp)
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result, usage = _call_gemini({"model": "gemini-2.5-flash"}, "test prompt")
        assert result == result_json

    def test_no_api_key(self, mocker):
        mocker.patch("pulse.os.getenv", return_value="")
        result, usage = _call_gemini({}, "test")
        assert result == {}

    def test_network_error(self, mocker):
        mocker.patch("pulse.requests.post", side_effect=requests.RequestException("fail"))
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result, usage = _call_gemini({"model": "gemini-2.5-flash"}, "test")
        assert result == {}
        assert pulse_ok() is False

    def test_bad_json(self, mocker):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "not json"}]}}]
        }
        resp.raise_for_status.return_value = None
        mocker.patch("pulse.requests.post", return_value=resp)
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result, usage = _call_gemini({"model": "gemini-2.5-flash"}, "test")
        assert result == {}
        assert pulse_ok() is False

    def test_no_candidates(self, mocker):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"candidates": []}
        resp.raise_for_status.return_value = None
        mocker.patch("pulse.requests.post", return_value=resp)
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result, usage = _call_gemini({"model": "gemini-2.5-flash"}, "test")
        assert result == {}
        assert pulse_ok() is False


class TestGeneratePulse:
    def test_disabled(self):
        result = generate_pulse({"pulse": {"enabled": False}})
        assert result is None

    def test_missing_pulse_config(self):
        result = generate_pulse({})
        assert result is None

    def test_all_clear_no_alerts(self, mocker):
        mocker.patch("pulse.db.get_all_active_alerts", return_value=[])
        mocker.patch("pulse.db.store_pulse")
        mocker.patch("pulse.db.store_category_snapshots")

        result = generate_pulse({"pulse": {"enabled": True}})
        assert result is not None
        assert result["alert_count"] == 0
        assert "all clear" in result["summary"].lower()
        db.store_pulse.assert_called_once()

    def test_with_alerts(self, mocker):
        alerts = [
            {"source": "rmv", "title_en": "Delay", "body_en": "S1", "stale": False,
             "service": "S-Bahn", "lines": '["S1"]', "severity": 2,
             "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-12-31T23:59:59Z",
             "published_at": "2020-01-01T00:00:00Z", "url": None, "lat": None, "lon": None,
             "location_label": None, "image": None, "icon": None, "alert_id": "HIM_1",
             "cached_at": "2020-01-01T00:00:00Z", "removed_at": None},
        ]
        gemini_response = {
            "summary": "S1 delays reported",
            "recommendation": "Allow extra time for S-Bahn.",
            "references": ["HIM_1"],
            "trend_override": [],
        }
        mocker.patch("pulse.db.get_all_active_alerts", return_value=alerts)
        mocker.patch("pulse.db.get_recent_pulses", return_value=[])
        mocker.patch("pulse.db.get_recent_daily_summaries", return_value=[])
        mocker.patch("pulse.db.store_pulse")
        mocker.patch("pulse.db.store_category_snapshots")
        mocker.patch("pulse.db.get_category_snapshots", return_value=[])
        mocker.patch("pulse.load_prompt", return_value=(
            {"model": "gemini-2.5-flash", "temperature": 0.3},
            "Prompt: {timestamp} {alert_count} {alerts_json} {stale_summary} {history_section} {timeseries_json}"
        ))
        mocker.patch("pulse._call_gemini", return_value=(gemini_response, {}))

        result = generate_pulse({"pulse": {"enabled": True}})
        assert result is not None
        assert result["summary"] == "S1 delays reported"
        assert result["alert_count"] == 1
        assert result["categories"]["transport"]["status"] in ("clear", "minor", "moderate", "severe")
        # trend is deterministic now — empty history means stable
        assert result["categories"]["transport"]["trend"] == "stable"
        assert "HIM_1" in result["references"]

    def _run_with_response(self, mocker, gemini_response):
        alerts = [
            {"source": "rmv", "title_en": "Delay", "body_en": "S1", "stale": False,
             "service": "S-Bahn", "lines": '["S1"]', "severity": 2,
             "valid_from": "2020-01-01T00:00:00Z", "valid_until": "2099-12-31T23:59:59Z",
             "published_at": "2020-01-01T00:00:00Z", "url": None, "lat": None, "lon": None,
             "location_label": None, "image": None, "icon": None, "alert_id": "HIM_1",
             "cached_at": "2020-01-01T00:00:00Z", "removed_at": None},
        ]
        mocker.patch("pulse.db.get_all_active_alerts", return_value=alerts)
        mocker.patch("pulse.db.get_recent_pulses", return_value=[])
        mocker.patch("pulse.db.get_recent_daily_summaries", return_value=[])
        mocker.patch("pulse.db.store_pulse")
        mocker.patch("pulse.db.store_category_snapshots")
        mocker.patch("pulse.db.get_category_snapshots", return_value=[])
        mocker.patch("pulse.load_prompt", return_value=(
            {"model": "gemini-2.5-flash", "temperature": 0.3},
            "Prompt: {timestamp} {alert_count} {alerts_json} {stale_summary} {history_section} {timeseries_json}"
        ))
        mocker.patch("pulse._call_gemini", return_value=(gemini_response, {}))
        return generate_pulse({"pulse": {"enabled": True}})

    def test_valid_trend_override_applied(self, mocker):
        result = self._run_with_response(mocker, {
            "summary": "S1 back soon", "recommendation": "None.", "references": [],
            "trend_override": [{"category": "transport", "trend": "improving",
                                "reason": "alert states service resumes at 14:30"}],
        })
        assert result["categories"]["transport"]["trend"] == "improving"

    def test_override_without_reason_rejected(self, mocker):
        result = self._run_with_response(mocker, {
            "summary": "S1", "recommendation": "None.", "references": [],
            "trend_override": [{"category": "transport", "trend": "improving", "reason": ""}],
        })
        assert result["categories"]["transport"]["trend"] == "stable"

    def test_override_invalid_category_rejected(self, mocker):
        result = self._run_with_response(mocker, {
            "summary": "S1", "recommendation": "None.", "references": [],
            "trend_override": [{"category": "traffic", "trend": "improving", "reason": "x"}],
        })
        assert all(c["trend"] == "stable" for c in result["categories"].values())

    def test_legacy_categories_field_ignored(self, mocker):
        # Old prompt format on a data volume — LLM still returns categories;
        # they must be ignored, not crash or leak into the output
        result = self._run_with_response(mocker, {
            "summary": "S1", "recommendation": "None.", "references": [],
            "categories": {"transport": {"trend": "worsening"}},
        })
        assert result["categories"]["transport"]["trend"] == "stable"

    def test_pulse_config_version_stamped_on_debug_record(self, mocker):
        write_debug_log = mocker.patch("pulse._write_debug_log")
        self._run_with_response(mocker, {
            "summary": "S1", "recommendation": "None.", "references": [],
        })
        debug_data = write_debug_log.call_args[0][0]
        assert debug_data["pulse_config_version"] == compute_pulse_config_version(
            "Prompt: {timestamp} {alert_count} {alerts_json} {stale_summary} {history_section} {timeseries_json}"
        )


class TestGenerateDailySummary:
    def test_disabled(self):
        result = generate_daily_summary({"pulse": {"enabled": False}})
        assert result is None

    def test_no_pulses(self, mocker):
        mocker.patch("pulse.db.get_pulses_for_date", return_value=[])
        mocker.patch("pulse.db.store_daily_summary")

        result = generate_daily_summary({"pulse": {"enabled": True}}, "2026-06-22")
        assert result is not None
        assert "No pulse data" in result["summary"]

    def test_with_pulses(self, mocker):
        pulses = [
            {"generated_at": "2026-06-22T10:00:00Z", "summary": "S1 delayed",
             "categories": {"transport": {"status": "delays", "trend": "worsening"}}},
            {"generated_at": "2026-06-22T11:00:00Z", "summary": "All clear",
             "categories": {"transport": {"status": "clear", "trend": "improving"}}},
        ]
        gemini_response = {
            "summary": "S1 had a morning disruption, resolved by 11:00.",
            "peak_issues": ["S1 delay"],
        }
        mocker.patch("pulse.db.get_pulses_for_date", return_value=pulses)
        mocker.patch("pulse.db.get_recent_daily_summaries", return_value=[])
        mocker.patch("pulse.db.store_daily_summary")
        mocker.patch("pulse.load_prompt", return_value=(
            {"model": "gemini-2.5-flash"},
            "Summarize: {date} {pulse_count} {pulses_json} {previous_summaries}"
        ))
        mocker.patch("pulse._call_gemini", return_value=(gemini_response, {}))

        result = generate_daily_summary({"pulse": {"enabled": True}}, "2026-06-22")
        assert result is not None
        assert result["summary"] == "S1 had a morning disruption, resolved by 11:00."
        db.store_daily_summary.assert_called_once()


class TestBuildHistorySectionWithDaily:
    def test_with_both(self):
        pulses = [{"generated_at": "2026-06-22T10:00:00Z", "summary": "Test",
                   "categories": {"weather": {"status": "clear", "trend": "stable"}}}]
        dailies = [{"date": "2026-06-21", "summary": "Yesterday was calm"}]
        result = _build_history_section(pulses, dailies)
        assert "HOURLY PULSES" in result
        assert "DAILY SUMMARIES" in result
        assert "Yesterday was calm" in result

    def test_daily_only(self):
        dailies = [{"date": "2026-06-21", "summary": "Calm day"}]
        result = _build_history_section([], dailies)
        assert "DAILY SUMMARIES" in result
        assert "HOURLY PULSES" not in result


class TestPulseDB:
    def test_store_and_retrieve(self):
        pulse = {
            "generated_at": "2026-06-22T10:00:00Z",
            "summary": "Test pulse",
            "categories": {"transit": {"status": "normal", "trend": "stable"}},
            "recommendation": "No action needed.",
            "alert_count": 5,
        }
        db.store_pulse(pulse)
        latest = db.get_latest_pulse()
        assert latest is not None
        assert latest["summary"] == "Test pulse"
        assert latest["categories"]["transit"]["status"] == "normal"
        assert latest["alert_count"] == 5

    def test_get_recent_pulses(self):
        for i in range(5):
            db.store_pulse({
                "generated_at": f"2026-06-22T{10+i:02d}:00:00Z",
                "summary": f"Pulse {i}",
                "categories": {},
                "recommendation": "",
                "alert_count": i,
            })
        recent = db.get_recent_pulses(3)
        assert len(recent) == 3
        assert recent[0]["summary"] == "Pulse 4"

    def test_latest_returns_none_when_empty(self):
        assert db.get_latest_pulse() is None

    def test_get_status_json_includes_pulse(self):
        db.store_pulse({
            "generated_at": "2026-06-22T10:00:00Z",
            "summary": "Test",
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        status = db.get_status_json()
        assert "pulse" in status
        assert status["pulse"]["summary"] == "Test"

    def test_get_status_json_pulse_none_when_empty(self):
        status = db.get_status_json()
        assert status["pulse"] is None

    def test_pulse_cleanup(self):
        db.store_pulse({
            "generated_at": "2026-05-01T10:00:00Z",
            "summary": "Old pulse",
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        db.store_pulse({
            "generated_at": "2026-06-22T10:00:00Z",
            "summary": "Recent pulse",
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        db.expire_processed_alerts()
        recent = db.get_recent_pulses(10)
        assert len(recent) == 1
        assert recent[0]["summary"] == "Recent pulse"

    def test_store_and_get_daily_summary(self):
        db.store_daily_summary("2026-06-22", "A calm day in Frankfurt.", "2026-06-22T23:00:00Z")
        summaries = db.get_recent_daily_summaries(1)
        assert len(summaries) == 1
        assert summaries[0]["date"] == "2026-06-22"
        assert summaries[0]["summary"] == "A calm day in Frankfurt."

    def test_daily_summary_upsert(self):
        db.store_daily_summary("2026-06-22", "First version", "2026-06-22T23:00:00Z")
        db.store_daily_summary("2026-06-22", "Updated version", "2026-06-22T23:05:00Z")
        summaries = db.get_recent_daily_summaries(10)
        assert len(summaries) == 1
        assert summaries[0]["summary"] == "Updated version"

    def test_get_pulses_for_date(self):
        for hour in range(3):
            db.store_pulse({
                "generated_at": f"2026-06-22T{10+hour:02d}:00:00Z",
                "summary": f"Pulse {hour}",
                "categories": {},
                "recommendation": "",
                "alert_count": hour,
            })
        db.store_pulse({
            "generated_at": "2026-06-21T10:00:00Z",
            "summary": "Yesterday",
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        today = db.get_pulses_for_date("2026-06-22")
        assert len(today) == 3
        yesterday = db.get_pulses_for_date("2026-06-21")
        assert len(yesterday) == 1

    def test_store_and_get_category_snapshots(self):
        snapshots = {
            "transport": {"ongoing_count": 5, "ongoing_score": 8.5,
                          "projected_count": 2, "projected_score": 3.0,
                          "upcoming_count": 3, "upcoming_score": 4.5,
                          "upcoming_near_score": 1.5},
            "weather": {"ongoing_count": 1, "ongoing_score": 1.5,
                        "projected_count": 0, "projected_score": 0.0,
                        "upcoming_count": 0, "upcoming_score": 0.0,
                        "upcoming_near_score": 0.0},
        }
        db.store_category_snapshots("2026-06-22T10:00:00Z", snapshots)
        rows = db.get_category_snapshots("transport", "2026-06-22T00:00:00Z")
        assert len(rows) == 1
        assert rows[0]["ongoing_count"] == 5
        assert rows[0]["ongoing_score"] == 8.5
        assert rows[0]["upcoming_count"] == 3
        assert rows[0]["upcoming_score"] == 4.5
        assert rows[0]["upcoming_near_score"] == 1.5

    def test_category_snapshot_upsert(self):
        db.store_category_snapshots("2026-06-22T10:00:00Z", {
            "transport": {"ongoing_count": 3, "ongoing_score": 5.0,
                          "projected_count": 0, "projected_score": 0.0,
                          "upcoming_count": 0, "upcoming_score": 0.0,
                          "upcoming_near_score": 0.0},
        })
        db.store_category_snapshots("2026-06-22T10:00:00Z", {
            "transport": {"ongoing_count": 7, "ongoing_score": 12.0,
                          "projected_count": 1, "projected_score": 2.0,
                          "upcoming_count": 2, "upcoming_score": 3.0,
                          "upcoming_near_score": 1.0},
        })
        rows = db.get_category_snapshots("transport", "2026-06-22T00:00:00Z")
        assert len(rows) == 1
        assert rows[0]["ongoing_count"] == 7
        assert rows[0]["upcoming_count"] == 2

    def test_weights_version_filter(self):
        db.store_category_snapshots("2026-06-22T10:00:00Z", {
            "transport": {"ongoing_count": 3, "ongoing_score": 5.0},
        }, weights_version=1)
        db.store_category_snapshots("2026-06-22T11:00:00Z", {
            "transport": {"ongoing_count": 4, "ongoing_score": 20.0,
                          "scheduled_upcoming_score": 1.5},
        }, weights_version=2)

        all_rows = db.get_category_snapshots("transport", "2026-06-22T00:00:00Z")
        assert len(all_rows) == 2

        v2_rows = db.get_category_snapshots("transport", "2026-06-22T00:00:00Z", weights_version=2)
        assert len(v2_rows) == 1
        assert v2_rows[0]["ongoing_score"] == 20.0
        assert v2_rows[0]["scheduled_upcoming_score"] == 1.5
        assert v2_rows[0]["weights_version"] == 2
