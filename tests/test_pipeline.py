import pytest


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
