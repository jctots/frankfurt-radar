import json

import pytest

import db
from notifier.bot import handle_update
from notifier.preferences import default_preferences


def _msg_update(chat_id, text):
    return {
        "update_id": 1,
        "message": {
            "message_id": 100,
            "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }


def _cb_update(chat_id, data, message_id=100):
    return {
        "update_id": 2,
        "callback_query": {
            "id": "cb_123",
            "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
            "data": data,
        },
    }


@pytest.fixture
def bot_config(config):
    config["admin_health_notifier"] = {"telegram_chat_id": 999}
    config["web"] = {"allow_manual_poll": True}
    return config


class TestStartCommand:
    def test_start_creates_subscriber(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        handle_update(_msg_update(1000, "/start"), bot_config)

        sub = db.get_subscriber_by_chat_id(1000)
        assert sub is not None
        assert sub["active"] == 1

    def test_start_sets_conversation_state(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        handle_update(_msg_update(1001, "/start"), bot_config)

        sub = db.get_subscriber_by_chat_id(1001)
        assert sub["conversation_state"] is not None
        assert sub["conversation_state"]["step"] == "sources"

    def test_start_sends_source_grid(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        handle_update(_msg_update(1002, "/start"), bot_config)

        assert mock_send.call_count == 1
        text = mock_send.call_args.args[1]
        assert "alert sources" in text.lower()

    def test_start_reactivates_stopped_subscriber(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        db.add_subscriber(1003)
        db.deactivate_subscriber(1003)
        assert db.get_subscriber_by_chat_id(1003)["active"] == 0

        handle_update(_msg_update(1003, "/start"), bot_config)

        sub = db.get_subscriber_by_chat_id(1003)
        assert sub["active"] == 1
        text = mock_send.call_args.args[1]
        assert "welcome back" in text.lower()
        assert sub["conversation_state"] is None

    def test_start_already_active(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        db.add_subscriber(1005)

        handle_update(_msg_update(1005, "/start"), bot_config)

        text = mock_send.call_args.args[1]
        assert "already subscribed" in text.lower()

    def test_settings_enters_onboarding(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        handle_update(_msg_update(1004, "/settings"), bot_config)

        sub = db.get_subscriber_by_chat_id(1004)
        assert sub is not None
        assert sub["conversation_state"]["step"] == "sources"


class TestStopCommand:
    def test_stop_deactivates(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        db.add_subscriber(2000)

        handle_update(_msg_update(2000, "/stop"), bot_config)

        sub = db.get_subscriber_by_chat_id(2000)
        assert sub["active"] == 0

    def test_stop_clears_conversation_state(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        db.add_subscriber(2001)
        db.set_conversation_state(2001, {"step": "sources", "prefs": {}})

        handle_update(_msg_update(2001, "/stop"), bot_config)

        sub = db.get_subscriber_by_chat_id(2001)
        assert sub["conversation_state"] is None

    def test_stop_when_not_subscribed(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        handle_update(_msg_update(2002, "/stop"), bot_config)

        text = mock_send.call_args.args[1]
        assert "not subscribed" in text.lower()


class TestDeleteDataCommand:
    def test_deletedata_removes_subscriber(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        db.add_subscriber(3000)

        handle_update(_msg_update(3000, "/deletedata"), bot_config)

        assert db.get_subscriber_by_chat_id(3000) is None

    def test_deletedata_when_not_subscribed(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        handle_update(_msg_update(3001, "/deletedata"), bot_config)

        text = mock_send.call_args.args[1]
        assert "no data" in text.lower()


class TestMyStatusCommand:
    def test_mystatus_shows_preferences(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        db.add_subscriber(4000)

        handle_update(_msg_update(4000, "/mystatus"), bot_config)

        text = mock_send.call_args.args[1]
        assert "preferences" in text.lower()
        assert "Transport" in text

    def test_mystatus_when_not_subscribed(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        handle_update(_msg_update(4001, "/mystatus"), bot_config)

        text = mock_send.call_args.args[1]
        assert "/start" in text


class TestHelpCommand:
    def test_help_shows_commands(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        handle_update(_msg_update(5000, "/help"), bot_config)

        text = mock_send.call_args.args[1]
        assert "/start" in text
        assert "/stop" in text
        assert "/deletedata" in text


class TestSourceToggle:
    def test_toggle_source_off(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(6000, "/start"), bot_config)

        handle_update(_cb_update(6000, "s:rmv"), bot_config)

        sub = db.get_subscriber_by_chat_id(6000)
        assert sub["conversation_state"]["prefs"]["sources"]["rmv"]["enabled"] is False

    def test_toggle_source_on_again(self, mocker, bot_config):
        mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(6001, "/start"), bot_config)

        handle_update(_cb_update(6001, "s:rmv"), bot_config)
        handle_update(_cb_update(6001, "s:rmv"), bot_config)

        sub = db.get_subscriber_by_chat_id(6001)
        assert sub["conversation_state"]["prefs"]["sources"]["rmv"]["enabled"] is True


class TestOnboardingFlow:
    def test_sources_done_advances_to_rmv_services(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7000, "/start"), bot_config)

        handle_update(_cb_update(7000, "s:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7000)
        assert sub["conversation_state"]["step"] == "rmv_services"

    def test_sources_done_skips_to_autobahn_when_rmv_off(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7001, "/start"), bot_config)

        handle_update(_cb_update(7001, "s:rmv"), bot_config)
        handle_update(_cb_update(7001, "s:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7001)
        assert sub["conversation_state"]["step"] == "autobahn_roads"

    def test_rmv_all_services_then_all_lines(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7002, "/start"), bot_config)

        handle_update(_cb_update(7002, "s:done"), bot_config)
        handle_update(_cb_update(7002, "rs:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7002)
        assert sub["conversation_state"]["step"] == "rmv_lines_choice"

        handle_update(_cb_update(7002, "rl:all"), bot_config)

        sub = db.get_subscriber_by_chat_id(7002)
        assert sub["conversation_state"]["step"] == "autobahn_roads"

    def test_rmv_specific_lines_text_input(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7003, "/start"), bot_config)

        handle_update(_cb_update(7003, "s:done"), bot_config)
        handle_update(_cb_update(7003, "rs:done"), bot_config)
        handle_update(_cb_update(7003, "rl:pick"), bot_config)

        sub = db.get_subscriber_by_chat_id(7003)
        assert sub["conversation_state"]["step"] == "rmv_lines_input"

        handle_update(_msg_update(7003, "S3, S5, Bus 32"), bot_config)

        sub = db.get_subscriber_by_chat_id(7003)
        assert sub["conversation_state"]["step"] == "rmv_lines_confirm"
        assert sub["conversation_state"]["prefs"]["sources"]["rmv"]["lines"] == ["S3", "S5", "Bus 32"]

    def test_full_onboarding_saves_preferences(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7005, "/start"), bot_config)

        # Disable everything except rmv
        for src in ["dwd", "polizei", "autobahn", "baustellen", "events", "sports"]:
            handle_update(_cb_update(7005, f"s:{src}"), bot_config)
        handle_update(_cb_update(7005, "s:done"), bot_config)

        # RMV: all services, all lines
        handle_update(_cb_update(7005, "rs:done"), bot_config)
        handle_update(_cb_update(7005, "rl:all"), bot_config)

        # Quiet hours: no
        handle_update(_cb_update(7005, "qh:no"), bot_config)
        # Pulse time: default
        handle_update(_cb_update(7005, "pt:12:00"), bot_config)
        # Keywords: skip
        handle_update(_cb_update(7005, "kw:skip"), bot_config)

        sub = db.get_subscriber_by_chat_id(7005)
        assert sub["conversation_state"] is None
        assert sub["preferences"]["sources"]["rmv"]["enabled"] is True
        assert sub["preferences"]["sources"]["dwd"]["enabled"] is False
        assert sub["preferences"]["quiet_hours"]["enabled"] is False
        assert sub["preferences"]["pulse_time"] == "12:00"

    def test_quiet_hours_preset(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7006, "/start"), bot_config)

        # Disable all sources to skip to quiet hours quickly
        for src in _all_sources():
            handle_update(_cb_update(7006, f"s:{src}"), bot_config)
        handle_update(_cb_update(7006, "s:done"), bot_config)

        handle_update(_cb_update(7006, "qh:yes"), bot_config)
        handle_update(_cb_update(7006, "pt:12:00"), bot_config)
        handle_update(_cb_update(7006, "kw:skip"), bot_config)

        sub = db.get_subscriber_by_chat_id(7006)
        assert sub["conversation_state"] is None
        assert sub["preferences"]["quiet_hours"]["enabled"] is True
        assert sub["preferences"]["quiet_hours"]["start"] == "22:00"
        assert sub["preferences"]["quiet_hours"]["end"] == "07:00"

    def test_completion_message_sent(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7007, "/start"), bot_config)

        for src in _all_sources():
            handle_update(_cb_update(7007, f"s:{src}"), bot_config)
        handle_update(_cb_update(7007, "s:done"), bot_config)
        handle_update(_cb_update(7007, "qh:no"), bot_config)
        handle_update(_cb_update(7007, "pt:12:00"), bot_config)
        handle_update(_cb_update(7007, "kw:skip"), bot_config)

        last_text = mock_send.call_args.args[1]
        assert "all set" in last_text.lower()


class TestAutobahnOnboarding:
    def test_autobahn_road_toggle(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7010, "/start"), bot_config)

        # Disable all except autobahn
        for src in ["rmv", "dwd", "polizei", "baustellen", "events", "sports"]:
            handle_update(_cb_update(7010, f"s:{src}"), bot_config)
        handle_update(_cb_update(7010, "s:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7010)
        assert sub["conversation_state"]["step"] == "autobahn_roads"

        handle_update(_cb_update(7010, "ar:A5"), bot_config)

        sub = db.get_subscriber_by_chat_id(7010)
        assert "A5" in sub["conversation_state"]["prefs"]["sources"]["autobahn"]["roads"]

        handle_update(_cb_update(7010, "ar:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7010)
        assert sub["conversation_state"]["step"] == "quiet_hours"


class TestBaustellenOnboarding:
    def test_baustellen_closures(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot._edit")
        mocker.patch("notifier.bot._answer_cb")
        handle_update(_msg_update(7020, "/start"), bot_config)

        for src in ["rmv", "dwd", "polizei", "autobahn", "events", "sports"]:
            handle_update(_cb_update(7020, f"s:{src}"), bot_config)
        handle_update(_cb_update(7020, "s:done"), bot_config)

        sub = db.get_subscriber_by_chat_id(7020)
        assert sub["conversation_state"]["step"] == "baustellen_closures"

        handle_update(_cb_update(7020, "bc:both"), bot_config)

        sub = db.get_subscriber_by_chat_id(7020)
        assert sub["conversation_state"]["step"] == "quiet_hours"
        assert sub["conversation_state"]["prefs"]["sources"]["baustellen"]["closures"] == ["full", "partial"]


class TestAdminCommands:
    def test_admin_status(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        db.set_meta("admin_health", json.dumps({"rmvPoller": True, "dwdPoller": False}))

        handle_update(_msg_update(999, "/status"), bot_config)

        text = mock_send.call_args.args[1]
        assert "Status" in text

    def test_admin_alerts(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mocker.patch("notifier.bot.get_status_json", return_value={
            "alerts": [{"source": "rmv"}, {"source": "rmv"}, {"source": "dwd"}],
        })

        handle_update(_msg_update(999, "/alerts"), bot_config)

        text = mock_send.call_args.args[1]
        assert "rmv: 2" in text

    def test_admin_poll(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        mock_post = mocker.patch("notifier.bot.requests.post")
        mock_post.return_value.status_code = 200
        mocker.patch.dict("os.environ", {"POLLER_TRIGGER_URL": "http://poller:8888/poll"})

        handle_update(_msg_update(999, "/poll"), bot_config)

        text = mock_send.call_args.args[1]
        assert "complete" in text.lower()

    def test_admin_visits(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")
        bot_config["web"] = {"umami_website_id": "test-id"}
        mocker.patch.dict("os.environ", {
            "UMAMI_INTERNAL_URL": "http://umami:3000",
            "UMAMI_USERNAME": "admin",
            "UMAMI_PASSWORD": "secret",
        })
        import notifier.bot as bot_mod
        mocker.patch.object(bot_mod, "_umami_token", "cached-token")

        month_resp = mocker.MagicMock(status_code=200)
        month_resp.json.return_value = {"visits": 100, "visitors": 50}
        day_resp = mocker.MagicMock(status_code=200)
        day_resp.json.return_value = {"visits": 10, "visitors": 5}
        active_resp = mocker.MagicMock(status_code=200)
        active_resp.json.return_value = [{"x": 2}]
        mocker.patch("notifier.bot.requests.get", side_effect=[month_resp, day_resp, active_resp])

        handle_update(_msg_update(999, "/visits"), bot_config)

        text = mock_send.call_args.args[1]
        assert "100" in text
        assert "50" in text
        assert "10" in text
        assert "5" in text
        assert "2" in text

    def test_non_admin_cant_use_admin_commands(self, mocker, bot_config):
        mock_send = mocker.patch("notifier.bot._send")

        handle_update(_msg_update(888, "/status"), bot_config)

        mock_send.assert_not_called()


class TestExpiredSession:
    def test_callback_with_no_state(self, mocker, bot_config):
        mock_answer = mocker.patch("notifier.bot._answer_cb")
        db.add_subscriber(8000)

        handle_update(_cb_update(8000, "s:rmv"), bot_config)

        mock_answer.assert_called_once()
        text = mock_answer.call_args.args[1]
        assert "expired" in text.lower()


def _all_sources():
    return ["rmv", "dwd", "polizei", "autobahn", "baustellen", "events", "sports", "strike", "feuerwehr"]
