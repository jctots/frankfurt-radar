import json
from datetime import datetime

import pytest

from zoneinfo import ZoneInfo

import db
from notifier.preferences import default_preferences
from notifier.subscriber_dispatch import dispatch_to_subscribers, flush_quiet_buffers


def _insert_alert(alert_id, source="rmv", title="Alert", body="Body", service=None,
                  lines=None, severity=None, cached_at=None):
    cached = cached_at or datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO alert_cache
               (alert_id, source, title_en, body_en, url, valid_until, service,
                lines, published_at, valid_from, severity, lat, lon, location_label,
                image, stale, icon, cached_at, removed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (alert_id, source, title, body, None, None, service,
             json.dumps(lines) if lines else None,
             datetime.now().isoformat(), None, severity,
             None, None, None, None, 0, None, cached, None),
        )


def _make_subscriber(chat_id, prefs=None):
    p = prefs or default_preferences()
    db.add_subscriber(chat_id, p)
    return db.get_subscriber_by_chat_id(chat_id)


def _alert_row(alert_id="A1", source="rmv", title="Delay", body="Details",
               service="S-Bahn", lines=None, severity=None):
    return {
        "alert_id": alert_id, "source": source, "title_en": title, "body_en": body,
        "url": None, "service": service, "lines": json.dumps(lines) if lines else None,
        "severity": severity,
    }


class TestDispatchToSubscribers:
    def test_matching_alert_sent(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        sub = _make_subscriber(100)
        rows = [_alert_row("A1", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 1
        mock_dm.assert_called_once()
        assert mock_dm.call_args.kwargs["chat_id"] == 100

    def test_non_matching_alert_filtered(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["sources"]["rmv"]["enabled"] = False
        _make_subscriber(101, prefs)
        rows = [_alert_row("A2", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 0
        mock_dm.assert_not_called()

    def test_dedup_via_sent_alerts(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        sub = _make_subscriber(102)
        db.record_sent_alert(sub["id"], "A3")
        rows = [_alert_row("A3", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 0
        mock_dm.assert_not_called()

    def test_quiet_hours_buffers_alert(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "00:00"
        prefs["quiet_hours"]["end"] = "23:59"
        sub = _make_subscriber(103, prefs)
        rows = [_alert_row("A4", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 0
        mock_dm.assert_not_called()
        buffered = db.flush_quiet_buffer(sub["id"])
        assert "A4" in buffered

    def test_blocked_subscriber_deactivated(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=False)
        _make_subscriber(104)
        rows = [_alert_row("A5", source="rmv")]

        dispatch_to_subscribers(rows, config)

        sub = db.get_subscriber_by_chat_id(104)
        assert sub["active"] == 0

    def test_sent_alert_recorded(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        sub = _make_subscriber(105)
        rows = [_alert_row("A6", source="rmv")]

        dispatch_to_subscribers(rows, config)

        unsent = db.get_unsent_for_subscriber(sub["id"], ["A6"])
        assert unsent == []

    def test_multiple_subscribers(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        _make_subscriber(200)
        _make_subscriber(201)
        rows = [_alert_row("M1", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 2
        assert mock_dm.call_count == 2

    def test_no_subscribers_returns_zero(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        rows = [_alert_row("X1", source="rmv")]

        count = dispatch_to_subscribers(rows, config)

        assert count == 0

    def test_multiple_alerts_filtered_per_subscriber(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["sources"]["dwd"]["enabled"] = False
        _make_subscriber(106, prefs)
        rows = [
            _alert_row("R1", source="rmv"),
            _alert_row("D1", source="dwd", severity=3),
        ]

        count = dispatch_to_subscribers(rows, config)

        assert count == 1
        assert mock_dm.call_args.kwargs["title"] == "🚇 Delay"


class TestFlushQuietBuffers:
    def test_briefing_with_missed_alerts(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        sub = _make_subscriber(300, prefs)

        _insert_alert("B1", source="rmv", title="S-Bahn delay")
        _insert_alert("B2", source="dwd", title="Storm warning")
        db.buffer_quiet_alert(sub["id"], "B1")
        db.buffer_quiet_alert(sub["id"], "B2")

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        count = flush_quiet_buffers(config)

        assert count == 1
        mock_dm.assert_called_once()
        assert "morning briefing" in mock_dm.call_args.kwargs["title"].lower()
        body = mock_dm.call_args.kwargs["body"]
        assert "S-Bahn delay" in body
        assert "Storm warning" in body

    def test_still_in_quiet_hours_no_flush(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "00:00"
        prefs["quiet_hours"]["end"] = "23:59"
        sub = _make_subscriber(301, prefs)

        db.buffer_quiet_alert(sub["id"], "B3")

        count = flush_quiet_buffers(config)

        assert count == 0
        mock_dm.assert_not_called()

    def test_no_quiet_hours_config_skips(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        _make_subscriber(302)

        count = flush_quiet_buffers(config)

        assert count == 0
        mock_dm.assert_not_called()

    def test_empty_buffer_still_sends_briefing(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        _make_subscriber(303, prefs)

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        count = flush_quiet_buffers(config)

        assert count == 1
        mock_dm.assert_called_once()
        body = mock_dm.call_args.kwargs["body"]
        assert "No alerts matching your filters" in body

    def test_blocked_on_flush_deactivates(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=False)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        _make_subscriber(304, prefs)

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        sub = db.get_subscriber_by_chat_id(304)
        assert sub["active"] == 0

    def test_flushed_alerts_recorded_as_sent(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        sub = _make_subscriber(305, prefs)

        _insert_alert("B5", source="rmv", title="Delay")
        db.buffer_quiet_alert(sub["id"], "B5")

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        unsent = db.get_unsent_for_subscriber(sub["id"], ["B5"])
        assert unsent == []

    def test_buffer_cleared_after_flush(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        sub = _make_subscriber(306, prefs)

        _insert_alert("B6", source="rmv", title="Delay")
        db.buffer_quiet_alert(sub["id"], "B6")

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        remaining = db.flush_quiet_buffer(sub["id"])
        assert remaining == []

    def test_briefing_not_resent_same_day(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        _make_subscriber(307, prefs)

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)
        assert mock_dm.call_count == 1

        flush_quiet_buffers(config)
        assert mock_dm.call_count == 1

    def test_briefing_includes_upcoming_events(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        _make_subscriber(308, prefs)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        berlin = ZoneInfo("Europe/Berlin")
        later_today = datetime.now(berlin).replace(hour=23, minute=0, second=0).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
        _insert_alert("F1", source="events", title="Frankfurt Marathon")
        with db._conn() as conn:
            conn.execute("UPDATE alert_cache SET valid_from = ? WHERE alert_id = 'F1'", (later_today,))

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        body = mock_dm.call_args.kwargs["body"]
        assert "Frankfurt Marathon" in body
        assert "Upcoming Today" in body

    def test_upcoming_filtered_by_preferences(self, mocker, config):
        mock_dm = mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        prefs["sources"]["events"]["enabled"] = False
        _make_subscriber(309, prefs)

        future = "2099-12-31T18:00:00Z"
        _insert_alert("F2", source="events", title="Concert")
        with db._conn() as conn:
            conn.execute("UPDATE alert_cache SET valid_from = ? WHERE alert_id = 'F2'", (future,))

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        body = mock_dm.call_args.kwargs["body"]
        assert "Concert" not in body
        assert "No events matching your filters today" in body

    def test_last_briefing_at_updated(self, mocker, config):
        mocker.patch("notifier.subscriber_dispatch.notify_subscriber_dm", return_value=True)
        prefs = default_preferences()
        prefs["quiet_hours"]["enabled"] = True
        prefs["quiet_hours"]["start"] = "22:00"
        prefs["quiet_hours"]["end"] = "07:00"
        sub = _make_subscriber(310, prefs)

        mocker.patch("notifier.subscriber_dispatch.is_quiet_hours", return_value=False)

        flush_quiet_buffers(config)

        updated = db.get_subscriber_by_chat_id(310)
        assert updated["last_briefing_at"] is not None
