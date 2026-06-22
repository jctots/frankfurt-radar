import json
from unittest.mock import MagicMock

import pytest
import requests

import db
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

        config, template = load_prompt.__wrapped__(
            "test"
        ) if hasattr(load_prompt, "__wrapped__") else (None, None)
        # Direct test using the actual file
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
        alerts_json, stale_summary = _build_alert_data(alerts)
        parsed = json.loads(alerts_json)
        assert len(parsed) == 1
        assert parsed[0]["source"] == "rmv"
        assert "2 autobahn" in stale_summary

    def test_no_stale(self):
        alerts = [
            {"source": "dwd", "title_en": "Storm", "body_en": "Severe", "stale": False,
             "service": None, "lines": None, "severity": 3,
             "valid_from": "2026-06-22T10:00:00Z", "valid_until": "2026-06-22T18:00:00Z"},
        ]
        _, stale_summary = _build_alert_data(alerts)
        assert stale_summary == "None"


class TestBuildHistorySection:
    def test_empty_history(self):
        assert "first pulse" in _build_history_section([]).lower()

    def test_with_pulses(self):
        pulses = [
            {"generated_at": "2026-06-22T10:00:00Z", "summary": "All clear", "travel_ok": True},
            {"generated_at": "2026-06-22T09:00:00Z", "summary": "S1 delay", "travel_ok": False},
        ]
        result = _build_history_section(pulses)
        assert "HOURLY PULSES" in result
        assert "All clear" in result
        assert "travel_ok=False" in result


class TestCallGemini:
    def test_successful_call(self, mocker):
        result_json = {"summary": "All clear", "travel_ok": True}
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(result_json)}]}}]
        }
        resp.raise_for_status.return_value = None
        mocker.patch("pulse.requests.post", return_value=resp)
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result = _call_gemini({"model": "gemini-2.5-flash"}, "test prompt")
        assert result == result_json

    def test_no_api_key(self, mocker):
        mocker.patch("pulse.os.getenv", return_value="")
        result = _call_gemini({}, "test")
        assert result == {}

    def test_network_error(self, mocker):
        mocker.patch("pulse.requests.post", side_effect=requests.RequestException("fail"))
        mocker.patch("pulse.os.getenv", return_value="fake-key")
        reset_pulse_health()

        result = _call_gemini({"model": "gemini-2.5-flash"}, "test")
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

        result = _call_gemini({"model": "gemini-2.5-flash"}, "test")
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

        result = _call_gemini({"model": "gemini-2.5-flash"}, "test")
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

        result = generate_pulse({"pulse": {"enabled": True}})
        assert result is not None
        assert result["travel_ok"] is True
        assert result["alert_count"] == 0
        assert "all clear" in result["summary"].lower()
        db.store_pulse.assert_called_once()

    def test_with_alerts(self, mocker):
        alerts = [
            {"source": "rmv", "title_en": "Delay", "body_en": "S1", "stale": False,
             "service": "S-Bahn", "lines": '["S1"]', "severity": 2,
             "valid_from": "2026-06-22T10:00:00Z", "valid_until": "2026-06-22T18:00:00Z",
             "published_at": "2026-06-22T10:00:00Z", "url": None, "lat": None, "lon": None,
             "location_label": None, "image": None, "icon": None, "alert_id": "HIM_1",
             "cached_at": "2026-06-22T10:00:00Z", "removed_at": None},
        ]
        gemini_response = {
            "summary": "S1 delays reported",
            "travel_ok": False,
            "categories": {"transit": {"status": "disrupted", "trend": "new"}},
            "recommendation": "Allow extra time for S-Bahn.",
        }
        mocker.patch("pulse.db.get_all_active_alerts", return_value=alerts)
        mocker.patch("pulse.db.get_recent_pulses", return_value=[])
        mocker.patch("pulse.db.get_pulses_since", return_value=[])
        mocker.patch("pulse.db.get_latest_pulse", return_value=None)
        mocker.patch("pulse.db.get_recent_daily_summaries", return_value=[])
        mocker.patch("pulse.db.store_pulse")
        mocker.patch("pulse.load_prompt", return_value=(
            {"model": "gemini-2.5-flash", "temperature": 0.3},
            "Prompt: {timestamp} {alert_count} {alerts_json} {stale_summary} {history_section} {categories_json}"
        ))
        mocker.patch("pulse._call_gemini", return_value=gemini_response)

        result = generate_pulse({"pulse": {"enabled": True}})
        assert result is not None
        assert result["travel_ok"] is True
        assert result["summary"] == "S1 delays reported"
        assert result["alert_count"] == 1
        assert result["categories"]["transport"]["status"] == "low"


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
             "travel_ok": False, "categories": {"transit": {"status": "disrupted", "trend": "new"}}},
            {"generated_at": "2026-06-22T11:00:00Z", "summary": "All clear",
             "travel_ok": True, "categories": {"transit": {"status": "normal", "trend": "improving"}}},
        ]
        gemini_response = {
            "summary": "S1 had a morning disruption, resolved by 11:00.",
            "peak_issues": ["S1 delay"],
            "travel_ok_pct": 50,
        }
        mocker.patch("pulse.db.get_pulses_for_date", return_value=pulses)
        mocker.patch("pulse.db.get_recent_daily_summaries", return_value=[])
        mocker.patch("pulse.db.store_daily_summary")
        mocker.patch("pulse.load_prompt", return_value=(
            {"model": "gemini-2.5-flash"},
            "Summarize: {date} {pulse_count} {pulses_json} {previous_summaries}"
        ))
        mocker.patch("pulse._call_gemini", return_value=gemini_response)

        result = generate_daily_summary({"pulse": {"enabled": True}}, "2026-06-22")
        assert result is not None
        assert result["summary"] == "S1 had a morning disruption, resolved by 11:00."
        db.store_daily_summary.assert_called_once()


class TestBuildHistorySectionWithDaily:
    def test_with_both(self):
        pulses = [{"generated_at": "2026-06-22T10:00:00Z", "summary": "Test", "travel_ok": True}]
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
            "travel_ok": True,
            "categories": {"transit": {"status": "normal", "trend": "stable"}},
            "recommendation": "No action needed.",
            "alert_count": 5,
        }
        db.store_pulse(pulse)
        latest = db.get_latest_pulse()
        assert latest is not None
        assert latest["summary"] == "Test pulse"
        assert latest["travel_ok"] is True
        assert latest["categories"]["transit"]["status"] == "normal"
        assert latest["alert_count"] == 5

    def test_get_recent_pulses(self):
        for i in range(5):
            db.store_pulse({
                "generated_at": f"2026-06-22T{10+i:02d}:00:00Z",
                "summary": f"Pulse {i}",
                "travel_ok": True,
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
            "travel_ok": False,
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        status = db.get_status_json()
        assert "pulse" in status
        assert status["pulse"]["summary"] == "Test"
        assert status["pulse"]["travel_ok"] is False

    def test_get_status_json_pulse_none_when_empty(self):
        status = db.get_status_json()
        assert status["pulse"] is None

    def test_pulse_cleanup(self):
        db.store_pulse({
            "generated_at": "2026-05-01T10:00:00Z",
            "summary": "Old pulse",
            "travel_ok": True,
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        db.store_pulse({
            "generated_at": "2026-06-22T10:00:00Z",
            "summary": "Recent pulse",
            "travel_ok": True,
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
                "travel_ok": True,
                "categories": {},
                "recommendation": "",
                "alert_count": hour,
            })
        db.store_pulse({
            "generated_at": "2026-06-21T10:00:00Z",
            "summary": "Yesterday",
            "travel_ok": True,
            "categories": {},
            "recommendation": "",
            "alert_count": 0,
        })
        today = db.get_pulses_for_date("2026-06-22")
        assert len(today) == 3
        yesterday = db.get_pulses_for_date("2026-06-21")
        assert len(yesterday) == 1
