"""Tests for MCP server API key authentication and rate limiting."""

import os
import sys
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))


async def _ok(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@pytest.fixture
def _reset_auth():
    """Save and restore auth module state around each test."""
    import auth as auth_mod

    orig_admin = auth_mod._admin_key
    orig_keys = auth_mod._api_keys
    orig_limit = auth_mod._rate_limit
    orig_window = auth_mod._rate_window
    orig_log = auth_mod._request_log.copy()
    orig_notify = auth_mod._last_notify.copy()
    yield auth_mod
    auth_mod._admin_key = orig_admin
    auth_mod._api_keys = orig_keys
    auth_mod._rate_limit = orig_limit
    auth_mod._rate_window = orig_window
    auth_mod._request_log.clear()
    auth_mod._request_log.update(orig_log)
    auth_mod._last_notify.clear()
    auth_mod._last_notify.update(orig_notify)


def _make_client(mod, *, admin_key=None, api_keys=None, rate_limit=60, rate_window=60):
    mod._admin_key = admin_key
    mod._api_keys = api_keys
    mod._rate_limit = rate_limit
    mod._rate_window = rate_window
    mod._request_log.clear()

    from auth import ApiKeyAuthMiddleware

    app = Starlette(
        routes=[Route("/test", _ok)],
        middleware=[Middleware(ApiKeyAuthMiddleware)],
    )
    return TestClient(app)


class TestNoAuth:
    def test_no_keys_configured_allows_all(self, _reset_auth):
        client = _make_client(_reset_auth)
        assert client.get("/test").status_code == 200

    def test_no_keys_configured_with_header_still_allowed(self, _reset_auth):
        client = _make_client(_reset_auth)
        resp = client.get("/test", headers={"Authorization": "Bearer anything"})
        assert resp.status_code == 200


class TestAdminKey:
    def test_admin_key_accepted(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin-secret")
        resp = client.get("/test", headers={"Authorization": "Bearer admin-secret"})
        assert resp.status_code == 200

    def test_admin_key_not_rate_limited(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin-secret", rate_limit=2, rate_window=60)
        for _ in range(10):
            resp = client.get("/test", headers={"Authorization": "Bearer admin-secret"})
            assert resp.status_code == 200

    def test_wrong_key_rejected_when_only_admin(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin-secret")
        resp = client.get("/test", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_missing_header_rejected_when_admin_set(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin-secret")
        resp = client.get("/test")
        assert resp.status_code == 401


class TestApiKeys:
    def test_valid_key_accepted(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1", "key-2"})
        resp = client.get("/test", headers={"Authorization": "Bearer key-1"})
        assert resp.status_code == 200

    def test_second_key_accepted(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1", "key-2"})
        resp = client.get("/test", headers={"Authorization": "Bearer key-2"})
        assert resp.status_code == 200

    def test_wrong_key_rejected(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"})
        resp = client.get("/test", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_missing_header_rejected(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"})
        resp = client.get("/test")
        assert resp.status_code == 401

    def test_malformed_header_rejected(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"})
        resp = client.get("/test", headers={"Authorization": "token key-1"})
        assert resp.status_code == 401

    def test_empty_bearer_rejected(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"})
        resp = client.get("/test", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401


class TestBothKeyTypes:
    def test_admin_and_api_keys_coexist(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin", api_keys={"user-1"})
        assert client.get("/test", headers={"Authorization": "Bearer admin"}).status_code == 200
        assert client.get("/test", headers={"Authorization": "Bearer user-1"}).status_code == 200

    def test_admin_key_not_in_api_keys_pool(self, _reset_auth):
        client = _make_client(_reset_auth, admin_key="admin", api_keys={"user-1"})
        assert client.get("/test", headers={"Authorization": "Bearer admin"}).status_code == 200


class TestRateLimiting:
    def test_rate_limit_enforced(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"}, rate_limit=3, rate_window=60)
        for _ in range(3):
            resp = client.get("/test", headers={"Authorization": "Bearer key-1"})
            assert resp.status_code == 200
        resp = client.get("/test", headers={"Authorization": "Bearer key-1"})
        assert resp.status_code == 429

    def test_rate_limit_per_key(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1", "key-2"}, rate_limit=2, rate_window=60)
        for _ in range(2):
            client.get("/test", headers={"Authorization": "Bearer key-1"})
        assert client.get("/test", headers={"Authorization": "Bearer key-1"}).status_code == 429
        assert client.get("/test", headers={"Authorization": "Bearer key-2"}).status_code == 200

    def test_rate_limit_response_has_retry_after(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"}, rate_limit=1, rate_window=30)
        client.get("/test", headers={"Authorization": "Bearer key-1"})
        resp = client.get("/test", headers={"Authorization": "Bearer key-1"})
        assert resp.status_code == 429
        assert resp.headers["retry-after"] == "30"

    def test_rate_limit_error_body(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"}, rate_limit=1, rate_window=60)
        client.get("/test", headers={"Authorization": "Bearer key-1"})
        resp = client.get("/test", headers={"Authorization": "Bearer key-1"})
        assert "Rate limit exceeded" in resp.json()["error"]


class TestRateLimitNotification:
    def test_notification_sent_on_rate_limit(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"}, rate_limit=1, rate_window=60)
        with patch("auth._notify_admin") as mock_notify:
            client.get("/test", headers={"Authorization": "Bearer key-1"})
            client.get("/test", headers={"Authorization": "Bearer key-1"})
            mock_notify.assert_called_once()
            title, body = mock_notify.call_args[0]
            assert "rate limit" in title.lower()
            assert "Key:" in body

    def test_notification_cooldown(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1"}, rate_limit=1, rate_window=60)
        with patch("auth._notify_admin") as mock_notify:
            for _ in range(5):
                client.get("/test", headers={"Authorization": "Bearer key-1"})
            assert mock_notify.call_count == 1

    def test_notification_per_key(self, _reset_auth):
        client = _make_client(_reset_auth, api_keys={"key-1", "key-2"}, rate_limit=1, rate_window=60)
        with patch("auth._notify_admin") as mock_notify:
            client.get("/test", headers={"Authorization": "Bearer key-1"})
            client.get("/test", headers={"Authorization": "Bearer key-1"})
            client.get("/test", headers={"Authorization": "Bearer key-2"})
            client.get("/test", headers={"Authorization": "Bearer key-2"})
            assert mock_notify.call_count == 2

    def test_key_is_masked_in_notification(self, _reset_auth):
        import auth as auth_mod
        assert auth_mod._mask_key("abcdefghijklmnop") == "abcd***mnop"
        assert auth_mod._mask_key("short") == "sh***"
