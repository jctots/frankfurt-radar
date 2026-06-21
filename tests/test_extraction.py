import json
from unittest.mock import MagicMock

import pytest
import requests

from extraction import extract_alert_details, extraction_ok, reset_extraction_health


class TestExtraction:
    def test_extract_returns_parsed_json(self, mocker):
        result_json = {"summary": "A strike", "valid_from": "2026-06-05T00:00:00+02:00"}
        resp = MagicMock()
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(result_json)}]}}]
        }
        resp.raise_for_status.return_value = None
        mocker.patch("extraction.requests.post", return_value=resp)
        mocker.patch("extraction.os.getenv", return_value="fake-key")

        result = extract_alert_details("some text", "extract fields")
        assert result == result_json

    def test_extract_fallback_on_network_error(self, mocker):
        mocker.patch("extraction.requests.post", side_effect=requests.RequestException("timeout"))
        mocker.patch("extraction.os.getenv", return_value="fake-key")
        reset_extraction_health()

        result = extract_alert_details("some text", "extract fields")
        assert result == {}
        assert extraction_ok() is False

    def test_extract_fallback_on_bad_json(self, mocker):
        resp = MagicMock()
        resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": "not valid json"}]}}]
        }
        resp.raise_for_status.return_value = None
        mocker.patch("extraction.requests.post", return_value=resp)
        mocker.patch("extraction.os.getenv", return_value="fake-key")
        reset_extraction_health()

        result = extract_alert_details("some text", "extract fields")
        assert result == {}
        assert extraction_ok() is False

    def test_extract_fallback_on_missing_key(self, mocker):
        mocker.patch("extraction.os.getenv", return_value="")
        reset_extraction_health()

        result = extract_alert_details("some text", "extract fields")
        assert result == {}
        assert extraction_ok() is True

    def test_health_tracking(self, mocker):
        reset_extraction_health()
        assert extraction_ok() is True

        mocker.patch("extraction.requests.post", side_effect=requests.RequestException("fail"))
        mocker.patch("extraction.os.getenv", return_value="fake-key")
        extract_alert_details("text", "prompt")
        assert extraction_ok() is False

        reset_extraction_health()
        assert extraction_ok() is True

    def test_extract_no_candidates(self, mocker):
        resp = MagicMock()
        resp.json.return_value = {"candidates": []}
        resp.raise_for_status.return_value = None
        mocker.patch("extraction.requests.post", return_value=resp)
        mocker.patch("extraction.os.getenv", return_value="fake-key")
        reset_extraction_health()

        result = extract_alert_details("some text", "extract fields")
        assert result == {}
        assert extraction_ok() is False
