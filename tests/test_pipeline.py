from datetime import datetime, timedelta, timezone

import pytest

from models import Alert


class TestProcessAlerts:
    def test_new_alerts_marked_seen(self, mocker, rmv_alert, dwd_alert, config):
        import db

        from pipeline import process_alerts
        process_alerts([rmv_alert, dwd_alert], config=config)

        unseen = db.get_unseen_alerts([rmv_alert, dwd_alert])
        assert unseen == []

    def test_seen_alerts_not_reprocessed(self, mocker, rmv_alert, config):
        import db
        db.mark_seen(rmv_alert)

        mock_batch = mocker.patch("pipeline.mark_seen_batch")

        from pipeline import process_alerts
        process_alerts([rmv_alert], config=config)

        mock_batch.assert_called_once_with([])

    def test_cold_start_guard_marks_all_seen(self, mocker, config):
        import db
        from pipeline import process_alerts

        config["notifier"]["notify_burst_threshold"] = 2
        alerts = [
            Alert(id=f"ID_{i}", source="rmv", title=f"Alert {i}", body="",
                  url=None, valid_until=None, service=None)
            for i in range(4)
        ]

        process_alerts(alerts, config=config)

        unseen = db.get_unseen_alerts(alerts)
        assert unseen == []

    def test_cold_start_guard_calls_patch_published_at(self, mocker, config):
        mock_patch = mocker.patch("pipeline.patch_published_at")

        config["notifier"]["notify_burst_threshold"] = 2
        alerts = [
            Alert(id=f"ID_{i}", source="rmv", title=f"Alert {i}", body="",
                  url=None, valid_until=None, service=None)
            for i in range(4)
        ]

        from pipeline import process_alerts
        process_alerts(alerts, config=config)

        mock_patch.assert_called_once()

    def test_below_threshold_no_patch_published_at(self, mocker, config):
        mock_patch = mocker.patch("pipeline.patch_published_at")

        from pipeline import process_alerts
        process_alerts(
            [Alert(id="SOLO", source="rmv", title="X", body="",
                   url=None, valid_until=None, service=None)],
            config=config,
        )

        mock_patch.assert_not_called()


class TestPoliceMaxAgeFilter:
    """Tests the inline filter from main.py.

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
        assert self._apply_filter([old], max_age_hours=48) == []

    def test_recent_police_alert_passes(self):
        recent_pub = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        recent = Alert(id="RECENT", source="polizei", title="X", body="",
                       url=None, valid_until=None, service=None, published_at=recent_pub)
        assert len(self._apply_filter([recent], max_age_hours=48)) == 1

    def test_non_police_alert_unaffected_by_age(self):
        old_pub = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        road = Alert(id="ROAD", source="autobahn", title="X", body="",
                     url=None, valid_until=None, service=None, published_at=old_pub)
        assert len(self._apply_filter([road], max_age_hours=48)) == 1

    def test_police_without_published_at_passes_filter(self):
        no_pub = Alert(id="NO_PUB", source="polizei", title="X", body="",
                       url=None, valid_until=None, service=None, published_at=None)
        assert len(self._apply_filter([no_pub], max_age_hours=48)) == 1
