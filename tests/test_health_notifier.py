import json

import pytest

import db


class TestCheckAndNotifyHealth:
    def test_degraded_triggers_alert(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"RMVPoller": False, "translator": True}))
        db.set_meta("prev_notified_health", json.dumps({"RMVPoller": True, "translator": True}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        assert mock_notify.call_count == 1
        title = mock_notify.call_args.args[0]
        assert "health alert" in title

    def test_recovered_triggers_recovery(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"RMVPoller": True}))
        db.set_meta("prev_notified_health", json.dumps({"RMVPoller": False}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        assert mock_notify.call_count == 1
        title = mock_notify.call_args.args[0]
        assert "recovered" in title

    def test_no_change_no_notification(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"RMVPoller": True}))
        db.set_meta("prev_notified_health", json.dumps({"RMVPoller": True}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        mock_notify.assert_not_called()

    def test_no_admin_health_meta_skips(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        mock_notify.assert_not_called()

    def test_no_config_section_skips(self, mocker, config):
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        mock_notify.assert_not_called()

    def test_first_degradation_without_prev_state(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"translator": False}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        assert mock_notify.call_count == 1
        body = mock_notify.call_args.args[1]
        assert "Translator" in body

    def test_prev_notified_health_updated(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"RMVPoller": False}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        stored = json.loads(db.get_meta("prev_notified_health"))
        assert stored == {"RMVPoller": False}

    def test_simultaneous_degraded_and_recovered(self, mocker, config):
        config["admin_health_notifier"] = {"backend": "telegram", "telegram_chat_id": "123"}
        mock_notify = mocker.patch("notifier.health.notify_admin_health")

        db.set_meta("admin_health", json.dumps({"RMVPoller": False, "translator": True}))
        db.set_meta("prev_notified_health", json.dumps({"RMVPoller": True, "translator": False}))

        from notifier.health import check_and_notify_health
        check_and_notify_health(config)

        assert mock_notify.call_count == 2
        titles = [call.args[0] for call in mock_notify.call_args_list]
        assert any("health alert" in t for t in titles)
        assert any("recovered" in t for t in titles)
