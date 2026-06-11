from unittest.mock import MagicMock

import pytest
import requests as req_lib

import notifications


def _mock_ok() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    return resp


class TestTelegram:
    def _telegram_config(self):
        return {
            "notifier": {
                "backend": "telegram",
                "telegram_channel": "@TestChannel",
            }
        }

    def test_sends_message(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(
            title="S-Bahn disruption",
            body="Delays on S1.",
            url="https://example.com",
            config=self._telegram_config(),
        )

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs["json"]
        assert payload["chat_id"] == "@TestChannel"
        assert "S-Bahn disruption" in payload["text"]
        assert payload["parse_mode"] == "HTML"

    def test_url_included_as_link(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(
            title="Alert", body="Body", url="https://rmv.de",
            config=self._telegram_config(),
        )

        payload = mock_post.call_args.kwargs["json"]
        assert "https://rmv.de" in payload["text"]

    def test_rmv_fallback_link_when_no_url(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(
            title="S-Bahn disruption", body="Delays on S1.", url=None,
            config=self._telegram_config(), source="rmv",
        )

        payload = mock_post.call_args.kwargs["json"]
        assert "rmv.de" in payload["text"]

    def test_dwd_fallback_link_when_no_url(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(
            title="Weather warning", body="Thunderstorm.", url=None,
            config=self._telegram_config(), source="dwd",
        )

        payload = mock_post.call_args.kwargs["json"]
        assert "dwd.de" in payload["text"]

    def test_no_request_when_token_missing(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(
            title="Alert", body="Body", url=None,
            config=self._telegram_config(),
        )

        mock_post.assert_not_called()

    def test_body_truncated_at_800_chars(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        long_body = "x" * 1000
        notifications.notify(title="T", body=long_body, url=None,
                             config=self._telegram_config())

        payload = mock_post.call_args.kwargs["json"]
        assert "…" in payload["text"]
        # Body portion should not exceed 800 chars + ellipsis
        assert len(payload["text"]) < 1000

    def test_html_escaped_in_title(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(title="Alert <test>", body="", url=None,
                             config=self._telegram_config())

        payload = mock_post.call_args.kwargs["json"]
        assert "<test>" not in payload["text"]
        assert "&lt;test&gt;" in payload["text"]


class TestNtfy:
    def _ntfy_config(self):
        return {
            "notifier": {
                "backend": "ntfy",
                "ntfy_url": "http://ntfy:80",
                "ntfy_topic": "test-topic",
            }
        }

    def test_sends_to_correct_url(self, mocker):
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(title="Alert", body="Body", url=None,
                             config=self._ntfy_config())

        mock_post.assert_called_once()
        assert mock_post.call_args.args[0] == "http://ntfy:80"

    def test_payload_structure(self, mocker):
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(title="Alert", body="Body text", url="https://rmv.de",
                             config=self._ntfy_config())

        payload = mock_post.call_args.kwargs["json"]
        assert payload["topic"] == "test-topic"
        assert payload["title"] == "Alert"
        assert payload["message"] == "Body text"
        assert payload["click"] == "https://rmv.de"

    def test_no_click_when_url_none(self, mocker):
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())

        notifications.notify(title="Alert", body="Body", url=None,
                             config=self._ntfy_config())

        payload = mock_post.call_args.kwargs["json"]
        assert "click" not in payload


class TestNotifyRouting:
    def test_ntfy_backend_routes_to_ntfy_url(self, mocker):
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())
        config = {"notifier": {"backend": "ntfy", "ntfy_url": "http://ntfy:80", "ntfy_topic": "t"}}

        notifications.notify(title="T", body="B", url=None, config=config)

        mock_post.assert_called_once()
        assert "ntfy" in mock_post.call_args.args[0]

    def test_telegram_backend_routes_to_telegram_api(self, mocker, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:token")
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())
        config = {"notifier": {"backend": "telegram", "telegram_channel": "@Ch"}}

        notifications.notify(title="T", body="B", url=None, config=config)

        mock_post.assert_called_once()
        assert "telegram.org" in mock_post.call_args.args[0]

    def test_unknown_backend_does_not_raise(self, mocker):
        mock_post = mocker.patch("notifications.requests.post", return_value=_mock_ok())
        config = {"notifier": {"backend": "unknown_backend"}}
        notifications.notify(title="T", body="B", url=None, config=config)
        mock_post.assert_not_called()
