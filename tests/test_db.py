from datetime import datetime, timedelta, timezone

import pytest

import db
from models import Alert


def _make_alert(id: str, valid_until=None, source="rmv") -> Alert:
    return Alert(
        id=id, source=source, title="Test", body="",
        url=None, valid_until=valid_until, service=None,
    )


class TestMeaningfulTextChanged:
    def test_identical_text_unchanged(self):
        assert db._meaningful_text_changed("Title", "Body text.", "Title", "Body text.") is False

    def test_stand_timestamp_ignored_in_title(self):
        assert db._meaningful_text_changed(
            "S1 delayed (Stand: 10:15 Uhr)", "Body", "S1 delayed (Stand: 09:00 Uhr)", "Body",
        ) is False

    def test_tiny_body_edit_below_threshold_not_meaningful(self):
        old = "Delays on the S1 line due to a signal fault near Hauptbahnhof."
        new = old + " "  # single trailing whitespace edit — negligible similarity change
        assert db._meaningful_text_changed("Title", new, "Title", old, min_change_ratio=0.10) is False

    def test_large_body_rewrite_above_threshold_is_meaningful(self):
        old = "Delays on the S1 line due to a signal fault."
        new = "Nationwide signal failure disrupts all regional and S-Bahn services across Hesse."
        assert db._meaningful_text_changed("Title", new, "Title", old, min_change_ratio=0.10) is True

    def test_default_threshold_used_when_omitted(self):
        old = "Delays on the S1 line due to a signal fault."
        new = old + "!"
        assert db._meaningful_text_changed("Title", new, "Title", old) is False


class TestGetUnseenAlerts:
    def test_all_new_when_none_seen(self, rmv_alert, dwd_alert):
        result = db.get_unseen_alerts([rmv_alert, dwd_alert])
        assert len(result) == 2

    def test_filters_out_seen_alert(self, rmv_alert, dwd_alert):
        db.mark_seen(rmv_alert)
        result = db.get_unseen_alerts([rmv_alert, dwd_alert])
        assert len(result) == 1
        assert result[0].id == dwd_alert.id

    def test_empty_input_returns_empty(self):
        assert db.get_unseen_alerts([]) == []

    def test_mark_seen_batch(self):
        alerts = [_make_alert(f"ID_{i}") for i in range(3)]
        db.mark_seen_batch(alerts)
        assert db.get_unseen_alerts(alerts) == []


class TestExpireProcessedAlerts:
    def test_expired_valid_until_removed(self):
        alert = _make_alert("EXPIRED", valid_until="2020-01-01T00:00")
        db.mark_seen(alert)

        db.expire_processed_alerts()

        remaining = db.get_unseen_alerts([alert])
        assert remaining == [alert]  # no longer in seen → returned as unseen

    def test_future_valid_until_kept(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        alert = _make_alert("FUTURE", valid_until=future)
        db.mark_seen(alert)

        db.expire_processed_alerts()

        remaining = db.get_unseen_alerts([alert])
        assert remaining == []  # still in seen → not returned

    def test_null_valid_until_old_entry_removed(self):
        alert = _make_alert("OLD_NULL")
        db.mark_seen(alert)

        # Backdate first_seen_at to 8 days ago to exceed the 7-day TTL
        eight_days_ago = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with db._conn() as conn:
            conn.execute(
                "UPDATE processed_alerts SET first_seen_at = ? WHERE alert_id = ?",
                (eight_days_ago, alert.id),
            )

        db.expire_processed_alerts()

        assert db.get_unseen_alerts([alert]) == [alert]  # removed → returned as unseen

    def test_null_valid_until_fresh_entry_kept(self):
        alert = _make_alert("FRESH_NULL")
        db.mark_seen(alert)

        db.expire_processed_alerts()

        assert db.get_unseen_alerts([alert]) == []  # still in seen


class TestAlertCache:
    def test_sync_populates_cache(self, rmv_alert, dwd_alert, mocker, config):
        mocker.patch("translation.translate_alert", return_value=("Title EN", "Body EN"))

        db.sync_alert_cache([rmv_alert, dwd_alert], config)

        status = db.get_status_json()
        assert len(status["alerts"]) == 2

    def test_sync_removes_stale_alerts(self, rmv_alert, dwd_alert, mocker, config):
        mocker.patch("translation.translate_alert", return_value=("T", "B"))

        db.sync_alert_cache([rmv_alert, dwd_alert], config)
        # Second poll — only dwd_alert remains active
        db.sync_alert_cache([dwd_alert], config)

        status = db.get_status_json()
        ids = [a["id"] for a in status["alerts"]]
        assert rmv_alert.id not in ids
        assert dwd_alert.id in ids

    def test_sync_updates_image_when_changed(self, rmv_alert, mocker, config):
        mocker.patch("translation.translate_alert", return_value=("T", "B"))
        rmv_alert.image = None
        db.sync_alert_cache([rmv_alert], config)

        rmv_alert.image = "https://example.com/img.jpg"
        db.sync_alert_cache([rmv_alert], config)

        with db._conn() as conn:
            row = conn.execute(
                "SELECT image FROM alert_cache WHERE alert_id = ?", (rmv_alert.id,)
            ).fetchone()
        assert row["image"] == "https://example.com/img.jpg"

    def test_sync_clears_image_when_set_to_none(self, rmv_alert, mocker, config):
        mocker.patch("translation.translate_alert", return_value=("T", "B"))
        rmv_alert.image = "https://example.com/img.jpg"
        db.sync_alert_cache([rmv_alert], config)

        rmv_alert.image = None
        db.sync_alert_cache([rmv_alert], config)

        with db._conn() as conn:
            row = conn.execute(
                "SELECT image FROM alert_cache WHERE alert_id = ?", (rmv_alert.id,)
            ).fetchone()
        assert row["image"] is None

    def test_dwd_alert_not_translated(self, dwd_alert, mocker, config):
        mock_translate = mocker.patch("translation.translate_alert", return_value=("T", "B"))

        db.sync_alert_cache([dwd_alert], config)

        # DWD skips translation in translate_alert — verify it was called (translation.py
        # handles the passthrough; db.sync_alert_cache calls translate_alert for all new alerts)
        mock_translate.assert_called_once()


class TestStrikeDuplicates:
    def test_mark_and_get_round_trip(self):
        db.mark_strike_duplicate("strike-a", "strike-b")
        marker = db.get_strike_duplicate("strike-a")
        assert marker["alert_id"] == "strike-a"
        assert marker["duplicate_of"] == "strike-b"
        assert marker["resolved_at"] is not None

    def test_unknown_alert_id_returns_none(self):
        assert db.get_strike_duplicate("strike-nonexistent") is None

    def test_survives_cleanup_when_target_only_soft_removed(self, rmv_alert, mocker, config):
        """Regression for the 2026-07-03 leak: a marker's target being
        soft-removed (removed_at set, e.g. by an alert_id scheme migration)
        must NOT make clear_expired_alerts() purge the marker — StrikePoller
        validates a marker's target via get_active_strikes(), which doesn't
        filter removed_at either. Using a stricter definition in the cleanup
        than in the check that trusts the marker meant every marker was
        deleted in the same poll cycle it was created.
        """
        mocker.patch("translation.translate_alert", return_value=("T", "B"))
        rmv_alert.source = "strike"
        db.sync_alert_cache([rmv_alert], config)
        with db._conn() as conn:
            conn.execute(
                "UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE alert_id = ?",
                (rmv_alert.id,),
            )

        db.mark_strike_duplicate("strike-dup", rmv_alert.id)
        db.clear_expired_alerts()

        assert db.get_strike_duplicate("strike-dup") is not None

    def test_purged_when_target_fully_gone(self):
        db.mark_strike_duplicate("strike-dup", "strike-never-cached")
        db.clear_expired_alerts()
        assert db.get_strike_duplicate("strike-dup") is None


class TestPatchPublishedAt:
    def _insert_cache_row(self, alert_id, source, valid_from, published_at=None):
        with db._conn() as conn:
            conn.execute(
                """INSERT INTO alert_cache (alert_id, source, title_en, body_en, valid_from, published_at)
                   VALUES (?, ?, 'T', 'B', ?, ?)""",
                (alert_id, source, valid_from, published_at),
            )

    def _get_published_at(self, alert_id):
        with db._conn() as conn:
            row = conn.execute(
                "SELECT published_at FROM alert_cache WHERE alert_id = ?", (alert_id,)
            ).fetchone()
        return row["published_at"] if row else None

    def test_autobahn_past_valid_from_corrected_to_valid_from(self):
        past = "2020-01-01T06:00:00Z"
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert_cache_row("AUTO_PAST", "autobahn", valid_from=past, published_at=now)

        db.patch_published_at()

        assert self._get_published_at("AUTO_PAST") == past

    def test_autobahn_future_valid_from_corrected_to_frankfurt_midnight(self):
        from zoneinfo import ZoneInfo
        future = (datetime.now(timezone.utc) + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._insert_cache_row("AUTO_FUTURE", "autobahn", valid_from=future, published_at=now)

        db.patch_published_at()

        result = self._get_published_at("AUTO_FUTURE")
        tz = ZoneInfo("Europe/Berlin")
        expected_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        expected_utc = expected_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert result == expected_utc

    def test_null_published_at_with_past_valid_from_gets_valid_from(self):
        past = "2020-06-01T10:00:00Z"
        self._insert_cache_row("NULL_PUB", "rmv", valid_from=past, published_at=None)

        db.patch_published_at()

        assert self._get_published_at("NULL_PUB") == past


class TestMeta:
    def test_set_and_get_meta(self):
        db.set_meta("last_polled_at", "2026-06-04T10:00:00Z")
        assert db.get_meta("last_polled_at") == "2026-06-04T10:00:00Z"

    def test_get_meta_missing_key_returns_none(self):
        assert db.get_meta("nonexistent_key") is None

    def test_set_meta_overwrites(self):
        db.set_meta("key", "v1")
        db.set_meta("key", "v2")
        assert db.get_meta("key") == "v2"


class TestSourceHealthInStatus:
    def test_source_health_included_when_set(self):
        import json
        health = {"RMVPoller": True, "DWDPoller": False}
        db.set_meta("source_health", json.dumps(health))

        status = db.get_status_json()

        assert status["source_health"] == health

    def test_source_health_empty_when_meta_missing(self):
        # clean_db fixture wipes meta — source_health key is absent
        status = db.get_status_json()

        assert status["source_health"] == {}
