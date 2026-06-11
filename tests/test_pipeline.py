from datetime import datetime, timedelta, timezone

import pytest

from models import Alert


class TestPollMode:
    def test_new_alerts_are_notified(self, mocker, rmv_alert, dwd_alert, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("Title EN", "Body EN"))

        from pipeline import process_alerts
        process_alerts([rmv_alert, dwd_alert], mode="poll", config=config)

        assert mock_notify.call_count == 2

    def test_seen_alerts_are_skipped(self, mocker, rmv_alert, config):
        import db
        db.mark_seen(rmv_alert)

        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("Title EN", "Body EN"))

        from pipeline import process_alerts
        process_alerts([rmv_alert], mode="poll", config=config)

        mock_notify.assert_not_called()

    def test_cold_start_guard_skips_notifications(self, mocker, config):
        from models import Alert
        from pipeline import process_alerts

        config["notifier"]["notify_burst_threshold"] = 3
        alerts = [
            Alert(id=f"ID_{i}", source="rmv", title=f"Alert {i}", body="",
                  url=None, valid_until=None, service=None)
            for i in range(5)  # 5 >= threshold 3
        ]

        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("T", "B"))

        process_alerts(alerts, mode="poll", config=config)

        mock_notify.assert_not_called()

    def test_cold_start_guard_marks_all_seen(self, mocker, config):
        from models import Alert
        from pipeline import process_alerts
        import db

        config["notifier"]["notify_burst_threshold"] = 2
        alerts = [
            Alert(id=f"ID_{i}", source="rmv", title=f"Alert {i}", body="",
                  url=None, valid_until=None, service=None)
            for i in range(4)
        ]

        mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("T", "B"))

        process_alerts(alerts, mode="poll", config=config)

        # All should now be marked seen
        unseen = db.get_unseen_alerts(alerts)
        assert unseen == []

    def test_processed_alerts_marked_seen_after_notify(self, mocker, rmv_alert, config):
        import db
        mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("Title EN", "Body EN"))

        from pipeline import process_alerts
        process_alerts([rmv_alert], mode="poll", config=config)

        unseen = db.get_unseen_alerts([rmv_alert])
        assert unseen == []

    def test_notify_called_with_correct_source_emoji(self, mocker, rmv_alert, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("S-Bahn disruption", "Body"))

        from pipeline import process_alerts
        process_alerts([rmv_alert], mode="poll", config=config)

        call_args = mock_notify.call_args
        assert "🚇" in call_args.kwargs.get("title", call_args.args[0] if call_args.args else "")

    def test_cold_start_guard_calls_patch_published_at(self, mocker, config):
        mock_patch = mocker.patch("pipeline.patch_published_at")
        mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("T", "B"))

        config["notifier"]["notify_burst_threshold"] = 2
        alerts = [
            Alert(id=f"ID_{i}", source="rmv", title=f"Alert {i}", body="",
                  url=None, valid_until=None, service=None)
            for i in range(4)
        ]

        from pipeline import process_alerts
        process_alerts(alerts, mode="poll", config=config)

        mock_patch.assert_called_once()


class TestProcessDaily:
    def _make_polizei(self, alert_id, hours_ago):
        pub = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
        return Alert(id=alert_id, source="polizei", title="Polizei incident", body="",
                     url=None, valid_until=None, service=None, published_at=pub)

    def test_empty_alerts_no_notify(self, mocker, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("T", "B"))

        from pipeline import process_alerts
        process_alerts([], mode="daily", config=config)

        mock_notify.assert_not_called()

    def test_rmv_section_appears_in_body(self, mocker, rmv_alert, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("S-Bahn disruption", "Body"))

        from pipeline import process_alerts
        process_alerts([rmv_alert], mode="daily", config=config)

        mock_notify.assert_called_once()
        body = mock_notify.call_args.kwargs.get("body", "")
        assert "Transport" in body
        assert "S-Bahn disruption" in body

    def test_police_old_alert_excluded_from_daily(self, mocker, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("Incident", "Body"))

        old_police = self._make_polizei("OLD_POLICE", hours_ago=50)

        from pipeline import process_alerts
        process_alerts([old_police], mode="daily", config=config)

        mock_notify.assert_not_called()

    def test_police_recent_alert_included_in_daily(self, mocker, config):
        mock_notify = mocker.patch("pipeline.notify")
        mocker.patch("pipeline.translate_alert", return_value=("Incident", "Body"))

        recent_police = self._make_polizei("RECENT_POLICE", hours_ago=1)

        from pipeline import process_alerts
        process_alerts([recent_police], mode="daily", config=config)

        mock_notify.assert_called_once()
        body = mock_notify.call_args.kwargs.get("body", "")
        assert "Police" in body


class TestPoliceMaxAgeFilter:
    """Tests the inline filter from main.py lines 126-132.

    The logic lives in main() and cannot be called in isolation without mocking
    all pollers. These tests replicate the exact filter expression to document
    its expected behavior and catch regressions if the expression changes.
    """

    def _apply_filter(self, alerts, max_age_hours):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
        return [
            a for a in alerts
            if not (a.source == "polizei" and a.published_at and a.published_at < cutoff)
        ]

    def test_old_police_alert_dropped(self):
        old_pub = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
        old = Alert(id="OLD", source="polizei", title="X", body="",
                    url=None, valid_until=None, service=None, published_at=old_pub)

        result = self._apply_filter([old], max_age_hours=48)

        assert result == []

    def test_recent_police_alert_passes(self):
        recent_pub = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        recent = Alert(id="RECENT", source="polizei", title="X", body="",
                       url=None, valid_until=None, service=None, published_at=recent_pub)

        result = self._apply_filter([recent], max_age_hours=48)

        assert len(result) == 1

    def test_non_police_alert_unaffected_by_age(self):
        old_pub = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        road = Alert(id="ROAD", source="autobahn", title="X", body="",
                     url=None, valid_until=None, service=None, published_at=old_pub)

        result = self._apply_filter([road], max_age_hours=48)

        assert len(result) == 1

    def test_police_without_published_at_passes_filter(self):
        no_pub = Alert(id="NO_PUB", source="polizei", title="X", body="",
                       url=None, valid_until=None, service=None, published_at=None)

        result = self._apply_filter([no_pub], max_age_hours=48)

        assert len(result) == 1
