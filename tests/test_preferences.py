import json
from datetime import datetime

import pytest

from zoneinfo import ZoneInfo

from notifier.preferences import default_preferences, is_quiet_hours, matches_preferences
import db


class TestDefaultPreferences:
    def test_returns_complete_schema(self):
        prefs = default_preferences()
        assert "sources" in prefs
        assert "quiet_hours" in prefs
        assert "language" in prefs

    def test_all_sources_enabled(self):
        prefs = default_preferences()
        for source, cfg in prefs["sources"].items():
            assert cfg["enabled"] is True, f"{source} should be enabled by default"

    def test_rmv_empty_filters(self):
        prefs = default_preferences()
        assert prefs["sources"]["rmv"]["services"] == []
        assert prefs["sources"]["rmv"]["lines"] == []

    def test_quiet_hours_disabled(self):
        prefs = default_preferences()
        assert prefs["quiet_hours"]["enabled"] is False


class TestMatchesPreferencesRMV:
    def _alert(self, **overrides):
        base = {"source": "rmv", "title_en": "Delay", "body_en": "...",
                "service": "S-Bahn", "lines": json.dumps(["S3", "S5"])}
        base.update(overrides)
        return base

    def test_enabled_no_filters_matches(self):
        prefs = default_preferences()
        assert matches_preferences(self._alert(), prefs) is True

    def test_disabled_source(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["enabled"] = False
        assert matches_preferences(self._alert(), prefs) is False

    def test_service_filter_matches(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["services"] = ["S-Bahn"]
        assert matches_preferences(self._alert(service="S-Bahn"), prefs) is True

    def test_service_filter_rejects(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["services"] = ["U-Bahn"]
        assert matches_preferences(self._alert(service="S-Bahn"), prefs) is False

    def test_service_filter_case_insensitive(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["services"] = ["s-bahn"]
        assert matches_preferences(self._alert(service="S-Bahn"), prefs) is True

    def test_line_filter_matches(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["lines"] = ["S3"]
        assert matches_preferences(self._alert(), prefs) is True

    def test_line_filter_rejects(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["lines"] = ["U4"]
        assert matches_preferences(self._alert(), prefs) is False

    def test_line_filter_case_insensitive(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["lines"] = ["s3"]
        assert matches_preferences(self._alert(), prefs) is True

    def test_empty_alert_lines_passes_line_filter(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["lines"] = ["S3"]
        assert matches_preferences(self._alert(lines=None), prefs) is True

    def test_lines_as_json_string(self):
        prefs = default_preferences()
        prefs["sources"]["rmv"]["lines"] = ["S5"]
        alert = self._alert(lines=json.dumps(["S3", "S5"]))
        assert matches_preferences(alert, prefs) is True


class TestMatchesPreferencesDWD:
    def _alert(self, severity=3):
        return {"source": "dwd", "title_en": "Storm", "body_en": "...", "severity": severity}

    def test_default_min_severity_matches_all(self):
        prefs = default_preferences()
        assert matches_preferences(self._alert(severity=1), prefs) is True

    def test_min_severity_filters(self):
        prefs = default_preferences()
        prefs["sources"]["dwd"]["min_severity"] = 3
        assert matches_preferences(self._alert(severity=2), prefs) is False
        assert matches_preferences(self._alert(severity=3), prefs) is True
        assert matches_preferences(self._alert(severity=4), prefs) is True

    def test_null_severity_passes(self):
        prefs = default_preferences()
        prefs["sources"]["dwd"]["min_severity"] = 3
        assert matches_preferences(self._alert(severity=None), prefs) is True

    def test_disabled(self):
        prefs = default_preferences()
        prefs["sources"]["dwd"]["enabled"] = False
        assert matches_preferences(self._alert(), prefs) is False


class TestMatchesPreferencesAutobahn:
    def _alert(self, title="A5 closure near Friedberg"):
        return {"source": "autobahn", "title_en": title, "body_en": ""}

    def test_no_road_filter_matches_all(self):
        prefs = default_preferences()
        assert matches_preferences(self._alert(), prefs) is True

    def test_road_filter_matches(self):
        prefs = default_preferences()
        prefs["sources"]["autobahn"]["roads"] = ["A5"]
        assert matches_preferences(self._alert(), prefs) is True

    def test_road_filter_rejects(self):
        prefs = default_preferences()
        prefs["sources"]["autobahn"]["roads"] = ["A3"]
        assert matches_preferences(self._alert(), prefs) is False

    def test_road_filter_case_insensitive(self):
        prefs = default_preferences()
        prefs["sources"]["autobahn"]["roads"] = ["a5"]
        assert matches_preferences(self._alert(), prefs) is True


class TestMatchesPreferencesBaustellen:
    def _alert(self, service="City (Full)"):
        return {"source": "baustellen", "title_en": "Road work", "body_en": "", "service": service}

    def test_default_full_closures(self):
        prefs = default_preferences()
        assert matches_preferences(self._alert(service="City (Full)"), prefs) is True
        assert matches_preferences(self._alert(service="City (Partial)"), prefs) is False

    def test_partial_only(self):
        prefs = default_preferences()
        prefs["sources"]["baustellen"]["closures"] = ["partial"]
        assert matches_preferences(self._alert(service="City (Partial)"), prefs) is True
        assert matches_preferences(self._alert(service="City (Full)"), prefs) is False

    def test_both(self):
        prefs = default_preferences()
        prefs["sources"]["baustellen"]["closures"] = ["full", "partial"]
        assert matches_preferences(self._alert(service="City (Full)"), prefs) is True
        assert matches_preferences(self._alert(service="City (Partial)"), prefs) is True


class TestMatchesPreferencesSimpleSources:
    def test_polizei_enabled(self):
        prefs = default_preferences()
        assert matches_preferences({"source": "polizei"}, prefs) is True

    def test_polizei_disabled(self):
        prefs = default_preferences()
        prefs["sources"]["polizei"]["enabled"] = False
        assert matches_preferences({"source": "polizei"}, prefs) is False

    def test_events_enabled(self):
        prefs = default_preferences()
        assert matches_preferences({"source": "events"}, prefs) is True

    def test_sports_enabled(self):
        prefs = default_preferences()
        assert matches_preferences({"source": "sports"}, prefs) is True

    def test_unknown_source_rejected(self):
        prefs = default_preferences()
        assert matches_preferences({"source": "unknown_source"}, prefs) is False


class TestIsQuietHours:
    def test_disabled(self):
        prefs = default_preferences()
        assert is_quiet_hours(prefs) is False

    def test_inside_quiet_hours(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 18, 23, 30, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is True

    def test_outside_quiet_hours(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 18, 12, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is False

    def test_midnight_wraparound_before_midnight(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 18, 23, 59, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is True

    def test_midnight_wraparound_after_midnight(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 19, 3, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is True

    def test_exactly_at_start(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 18, 22, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is True

    def test_exactly_at_end(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        now = datetime(2026, 6, 19, 7, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now) is False

    def test_same_day_range(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "13:00"
        prefs["quiet_hours"]["end"] = "15:00"
        now_in = datetime(2026, 6, 18, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        now_out = datetime(2026, 6, 18, 16, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert is_quiet_hours(prefs, now=now_in) is True
        assert is_quiet_hours(prefs, now=now_out) is False

    def test_different_timezone(self):
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        prefs["quiet_hours"]["timezone"] = "Asia/Manila"
        now = datetime(2026, 6, 18, 23, 0, tzinfo=ZoneInfo("Asia/Manila"))
        assert is_quiet_hours(prefs, now=now) is True


class TestDBSubscriberFunctions:
    def test_add_subscriber_uses_default_preferences(self):
        db.add_subscriber(12345)
        sub = db.get_subscriber_by_chat_id(12345)
        assert sub is not None
        assert "sources" in sub["preferences"]
        assert sub["preferences"]["sources"]["rmv"]["enabled"] is True

    def test_get_subscriber_by_chat_id(self):
        db.add_subscriber(111)
        sub = db.get_subscriber_by_chat_id(111)
        assert sub["chat_id"] == 111
        assert sub["active"] == 1
        assert sub["conversation_state"] is None

    def test_get_subscriber_by_chat_id_not_found(self):
        assert db.get_subscriber_by_chat_id(999) is None

    def test_update_subscriber_preferences(self):
        db.add_subscriber(222)
        new_prefs = default_preferences()
        new_prefs["sources"]["rmv"]["enabled"] = False
        db.update_subscriber_preferences(222, new_prefs)
        sub = db.get_subscriber_by_chat_id(222)
        assert sub["preferences"]["sources"]["rmv"]["enabled"] is False

    def test_record_and_get_unsent(self):
        db.add_subscriber(333)
        sub = db.get_subscriber_by_chat_id(333)
        db.record_sent_alert(sub["id"], "ALERT_A")
        db.record_sent_alert(sub["id"], "ALERT_B")
        unsent = db.get_unsent_for_subscriber(sub["id"], ["ALERT_A", "ALERT_B", "ALERT_C"])
        assert unsent == ["ALERT_C"]

    def test_get_unsent_empty_list(self):
        assert db.get_unsent_for_subscriber(1, []) == []

    def test_record_sent_alert_idempotent(self):
        db.add_subscriber(444)
        sub = db.get_subscriber_by_chat_id(444)
        db.record_sent_alert(sub["id"], "DUP")
        db.record_sent_alert(sub["id"], "DUP")
        unsent = db.get_unsent_for_subscriber(sub["id"], ["DUP"])
        assert unsent == []

    def test_buffer_and_flush_quiet(self):
        db.add_subscriber(555)
        sub = db.get_subscriber_by_chat_id(555)
        db.buffer_quiet_alert(sub["id"], "Q1")
        db.buffer_quiet_alert(sub["id"], "Q2")
        flushed = db.flush_quiet_buffer(sub["id"])
        assert flushed == ["Q1", "Q2"]
        assert db.flush_quiet_buffer(sub["id"]) == []

    def test_buffer_quiet_alert_idempotent(self):
        db.add_subscriber(666)
        sub = db.get_subscriber_by_chat_id(666)
        db.buffer_quiet_alert(sub["id"], "Q1")
        db.buffer_quiet_alert(sub["id"], "Q1")
        flushed = db.flush_quiet_buffer(sub["id"])
        assert flushed == ["Q1"]

    def test_cascade_delete_clears_quiet_buffer(self):
        db.add_subscriber(777)
        sub = db.get_subscriber_by_chat_id(777)
        db.buffer_quiet_alert(sub["id"], "Q1")
        db.remove_subscriber(777)
        with db._conn() as conn:
            rows = conn.execute("SELECT * FROM quiet_buffer WHERE subscriber_id = ?", (sub["id"],)).fetchall()
        assert len(rows) == 0
