import json
from datetime import datetime, timedelta, timezone

import pytest

import db


def _insert_alert(alert_id, source="rmv", title="Alert", body="Body", stale=0,
                  removed_at=None, cached_at=None, url=None, valid_from=None,
                  valid_until=None, service=None, lines=None, icon=None):
    cached = cached_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with db._conn() as conn:
        conn.execute(
            """INSERT INTO alert_cache
               (alert_id, source, title_en, body_en, url, valid_until, service,
                lines, published_at, valid_from, severity, lat, lon, location_label,
                image, stale, icon, cached_at, removed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (alert_id, source, title, body, url, valid_until, service,
             json.dumps(lines) if lines else None,
             datetime.now(timezone.utc).isoformat(), valid_from, None,
             None, None, None, None, stale, icon, cached, removed_at),
        )


class TestDispatchNewAlerts:
    def test_cursor_advances_after_dispatch(self, mocker, config):
        mocker.patch("notifier.dispatcher.notify")
        _insert_alert("A1", cached_at="2026-01-01T00:00:00.000Z")
        _insert_alert("A2", cached_at="2026-01-01T00:01:00.000Z")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 2
        assert db.get_meta("last_notified_at") == "2026-01-01T00:01:00.000Z"

    def test_only_new_alerts_dispatched_after_cursor(self, mocker, config):
        mocker.patch("notifier.dispatcher.notify")
        db.set_meta("last_notified_at", "2026-01-01T00:00:30.000Z")
        _insert_alert("OLD", cached_at="2026-01-01T00:00:00.000Z")
        _insert_alert("NEW", cached_at="2026-01-01T00:01:00.000Z")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 1

    def test_cold_start_guard_skips_notifications(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        config["notifier"]["notify_burst_threshold"] = 3
        for i in range(5):
            _insert_alert(f"BURST_{i}", cached_at=f"2026-01-01T00:00:0{i}.000Z")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 0
        mock_notify.assert_not_called()

    def test_cold_start_guard_advances_cursor(self, mocker, config):
        mocker.patch("notifier.dispatcher.notify")
        config["notifier"]["notify_burst_threshold"] = 2
        _insert_alert("B1", cached_at="2026-01-01T00:00:00.000Z")
        _insert_alert("B2", cached_at="2026-01-01T00:00:01.000Z")
        _insert_alert("B3", cached_at="2026-01-01T00:00:02.000Z")

        from notifier.dispatcher import dispatch_new_alerts
        dispatch_new_alerts(config)

        assert db.get_meta("last_notified_at") == "2026-01-01T00:00:02.000Z"

    def test_stale_alerts_excluded(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("FRESH", stale=0)
        _insert_alert("STALE", stale=1)

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 1
        assert mock_notify.call_count == 1

    def test_removed_alerts_excluded(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("ACTIVE")
        _insert_alert("GONE", removed_at="2026-01-01T00:00:00.000Z")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 1

    def test_disabled_sources_not_dispatched(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        config["notifier"]["disabled_sources"] = ["polizei"]
        config["notifier"]["notify_burst_threshold"] = 15
        _insert_alert("RMV1", source="rmv")
        _insert_alert("POL1", source="polizei")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 1
        call_kwargs = mock_notify.call_args.kwargs
        assert call_kwargs["source"] == "rmv"

    def test_no_alerts_returns_zero(self, mocker, config):
        mocker.patch("notifier.dispatcher.notify")

        from notifier.dispatcher import dispatch_new_alerts
        count = dispatch_new_alerts(config)

        assert count == 0

    def test_event_meta_included_in_body(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert(
            "EVT1", source="events", title="Street Fest", body="Fun times",
            valid_from="2026-06-20T00:00:00+00:00",
            valid_until="2026-06-22T23:59:00+00:00",
        )

        from notifier.dispatcher import dispatch_new_alerts
        dispatch_new_alerts(config)

        body = mock_notify.call_args.kwargs["body"]
        assert "20 Jun" in body or "Jun" in body

    def test_emoji_in_title(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("RMV2", source="rmv", title="S-Bahn disruption")

        from notifier.dispatcher import dispatch_new_alerts
        dispatch_new_alerts(config)

        title = mock_notify.call_args.kwargs["title"]
        assert "\U0001f687" in title


class TestDispatchDailySummary:
    def test_empty_cache_returns_false(self, mocker, config):
        mocker.patch("notifier.dispatcher.notify")

        from notifier.dispatcher import dispatch_daily_summary
        result = dispatch_daily_summary(config)

        assert result is False

    def test_groups_by_source(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("R1", source="rmv", title="S-Bahn delay")
        _insert_alert("W1", source="dwd", title="Storm warning")

        from notifier.dispatcher import dispatch_daily_summary
        result = dispatch_daily_summary(config)

        assert result is True
        body = mock_notify.call_args.kwargs["body"]
        assert "Transport" in body
        assert "Weather" in body

    def test_disabled_sources_excluded(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        config["notifier"]["disabled_sources"] = ["polizei"]
        _insert_alert("R1", source="rmv", title="Delay")
        _insert_alert("P1", source="polizei", title="Incident")

        from notifier.dispatcher import dispatch_daily_summary
        dispatch_daily_summary(config)

        body = mock_notify.call_args.kwargs["body"]
        assert "Police" not in body
        assert "Transport" in body

    def test_stale_alerts_included_in_daily(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("S1", source="rmv", title="Long disruption", stale=1)

        from notifier.dispatcher import dispatch_daily_summary
        result = dispatch_daily_summary(config)

        assert result is True

    def test_title_contains_date(self, mocker, config):
        mock_notify = mocker.patch("notifier.dispatcher.notify")
        _insert_alert("R1", source="rmv", title="Delay")

        from notifier.dispatcher import dispatch_daily_summary
        dispatch_daily_summary(config)

        title = mock_notify.call_args.kwargs["title"]
        assert "Frankfurt Radar" in title
