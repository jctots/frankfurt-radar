from unittest.mock import MagicMock

import pytest
import requests as req_lib

import translation


def _mock_ok(translated: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"translatedText": translated}
    return resp


def _mock_google_ok(translated: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"translations": [{"translatedText": translated}]}}
    return resp


class TestLibreTranslate:
    def test_happy_path(self, mocker, config):
        mocker.patch("translation.requests.post", return_value=_mock_ok("Train disruption"))
        result = translation.translate("Zugstörung", config)
        assert result == "Train disruption"

    def test_network_error_returns_original(self, mocker, config):
        mocker.patch("translation.requests.post",
                     side_effect=req_lib.RequestException("connection refused"))
        result = translation.translate("Zugstörung", config)
        assert result == "Zugstörung"

    def test_calls_correct_endpoint(self, mocker, config):
        mock_post = mocker.patch("translation.requests.post", return_value=_mock_ok("x"))
        translation.translate("text", config)
        call_url = mock_post.call_args.args[0]
        assert "localhost:5000" in call_url
        assert "/translate" in call_url


class TestGoogleTranslate:
    def test_happy_path(self, mocker, config):
        config["translator"]["backend"] = "google"
        mocker.patch("translation.requests.post", return_value=_mock_google_ok("Train disruption"))
        result = translation.translate("Zugstörung", config)
        assert result == "Train disruption"

    def test_missing_api_key_returns_original(self, mocker, config, monkeypatch):
        config["translator"]["backend"] = "google"
        monkeypatch.setenv("GOOGLE_TRANSLATE_API_KEY", "")
        result = translation.translate("Zugstörung", config)
        assert result == "Zugstörung"

    def test_api_error_returns_original(self, mocker, config):
        config["translator"]["backend"] = "google"
        mocker.patch("translation.requests.post",
                     side_effect=req_lib.RequestException("403"))
        result = translation.translate("Zugstörung", config)
        assert result == "Zugstörung"


class TestTranslateAlert:
    def test_dwd_alert_translated(self, mocker, dwd_alert, config):
        mock_translate = mocker.patch("translation.translate", return_value="Translated")
        en_title, en_body = translation.translate_alert(dwd_alert, config)
        assert mock_translate.call_count == 2
        assert en_title == "Translated"

    def test_events_alert_skips_translation(self, mocker, events_alert, config):
        mock_translate = mocker.patch("translation.translate")
        en_title, en_body = translation.translate_alert(events_alert, config)
        mock_translate.assert_not_called()
        assert en_title == events_alert.title
        assert en_body == events_alert.body

    def test_sports_alert_skips_translation(self, mocker, sports_alert, config):
        mock_translate = mocker.patch("translation.translate")
        en_title, en_body = translation.translate_alert(sports_alert, config)
        mock_translate.assert_not_called()
        assert en_title == sports_alert.title
        assert en_body == sports_alert.body

    def test_umlaut_transliteration_applied(self, mocker, rmv_alert, config):
        mocker.patch("translation.translate", return_value="Züge verspätet")
        en_title, _ = translation.translate_alert(rmv_alert, config)
        assert "Züge" not in en_title
        assert "Zuege" in en_title or "verspae" in en_title

    def test_polizei_title_location_preserved(self, mocker, polizei_alert, config):
        mocker.patch("translation.translate", return_value="Traffic accident")
        en_title, _ = translation.translate_alert(polizei_alert, config)
        # "Sachsenhausen: Verkehrsunfall" → location part preserved, event part translated
        assert "Sachsenhausen" in en_title
        assert "Traffic accident" in en_title
