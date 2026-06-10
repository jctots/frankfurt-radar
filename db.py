import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

log = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("DATA_DIR", ".")) / "radar.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER UNIQUE NOT NULL,
    preferences TEXT NOT NULL DEFAULT '{"rmv":true,"dwd":true,"polizei":true}',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS processed_alerts (
    alert_id      TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    valid_until   TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS alert_cache (
    alert_id       TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    title_en       TEXT NOT NULL,
    body_en        TEXT NOT NULL,
    url            TEXT,
    valid_until    TEXT,
    service        TEXT,
    lines          TEXT,
    published_at   TEXT,
    valid_from     TEXT,
    severity       INTEGER,
    lat            REAL,
    lon            REAL,
    location_label TEXT,
    image          TEXT,
    cached_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    removed_at     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_alerts (
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    alert_id      TEXT NOT NULL,
    sent_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (subscriber_id, alert_id)
);
"""


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        try:
            conn.execute("ALTER TABLE alert_cache ADD COLUMN valid_from TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE alert_cache ADD COLUMN image TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE alert_cache ADD COLUMN removed_at TEXT")
        except Exception:
            pass
    log.info("DB ready: %s", DB_PATH)


# ── processed_alerts (replaces seen.json) ────────────────────────────────────

def get_unseen_alerts(alerts: list) -> list:
    if not alerts:
        return []
    ids = [a.id for a in alerts]
    ph = ",".join("?" * len(ids))
    with _conn() as conn:
        seen = {r[0] for r in conn.execute(
            f"SELECT alert_id FROM processed_alerts WHERE alert_id IN ({ph})", ids
        )}
    return [a for a in alerts if a.id not in seen]


def mark_seen(alert: "Alert") -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO processed_alerts (alert_id, source, valid_until) VALUES (?, ?, ?)",
            (alert.id, alert.source, alert.valid_until),
        )


def mark_seen_batch(alerts: list) -> None:
    if not alerts:
        return
    with _conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO processed_alerts (alert_id, source, valid_until) VALUES (?, ?, ?)",
            [(a.id, a.source, a.valid_until) for a in alerts],
        )


def expire_processed_alerts() -> None:
    now = datetime.now(timezone.utc)
    exp = (now - timedelta(hours=1)).isoformat()
    ttl = (now - timedelta(days=7)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """DELETE FROM processed_alerts WHERE
               (valid_until IS NOT NULL AND valid_until < ?)
               OR (valid_until IS NULL AND first_seen_at < ?)""",
            (exp, ttl),
        )
        if cur.rowcount:
            log.info("Expired %d processed_alerts entries", cur.rowcount)


# ── alert_cache (replaces status.json) ───────────────────────────────────────

def sync_alert_cache(alerts: list, config: dict) -> None:
    """Translate new alerts and sync the cache to match the current fetch result."""
    from translation import translate_alert

    if not alerts:
        with _conn() as conn:
            conn.execute(
                "UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE removed_at IS NULL"
            )
            conn.execute(
                "DELETE FROM alert_cache WHERE removed_at IS NOT NULL"
                " AND removed_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', 'start of day')"
            )
        return

    current_ids = [a.id for a in alerts]
    ph = ",".join("?" * len(current_ids))

    # Determine which alerts already have a cached translation (active only)
    with _conn() as conn:
        cached = {r["alert_id"]: r["image"] for r in conn.execute(
            f"SELECT alert_id, image FROM alert_cache WHERE alert_id IN ({ph}) AND removed_at IS NULL", current_ids
        )}

    # Translate outside the connection — avoids holding a write lock during HTTP calls
    to_insert = []
    to_update_image = []
    for alert in alerts:
        if alert.id not in cached:
            en_title, en_body = translate_alert(alert, config)
            to_insert.append((
                alert.id, alert.source, en_title, en_body, alert.url,
                alert.valid_until, alert.service,
                json.dumps(alert.lines) if alert.lines else None,
                alert.published_at, alert.valid_from, alert.severity,
                alert.lat, alert.lon, alert.location_label, alert.image,
            ))
        elif cached[alert.id] != alert.image:
            to_update_image.append((alert.image, alert.id))

    # Batch write: insert new + refresh changed images + remove stale alerts
    with _conn() as conn:
        if to_insert:
            conn.executemany(
                """INSERT OR REPLACE INTO alert_cache
                   (alert_id, source, title_en, body_en, url, valid_until, service,
                    lines, published_at, valid_from, severity, lat, lon, location_label, image, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                to_insert,
            )
        if to_update_image:
            conn.executemany(
                "UPDATE alert_cache SET image = ? WHERE alert_id = ?",
                to_update_image,
            )
        # Mark stale active alerts as removed (keep for rest of day)
        conn.execute(
            f"UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            f" WHERE alert_id NOT IN ({ph}) AND removed_at IS NULL",
            current_ids
        )
        # Delete removed alerts from previous days
        conn.execute(
            "DELETE FROM alert_cache WHERE removed_at IS NOT NULL"
            " AND removed_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', 'start of day')"
        )

    log.info("alert_cache: %d total (%d cached, %d translated, %d image updated)",
             len(alerts), len(cached), len(to_insert), len(to_update_image))


def get_status_json() -> dict:
    """Return {updated_at, alerts, removed_alerts, source_health}."""
    with _conn() as conn:
        active_rows = conn.execute(
            "SELECT * FROM alert_cache WHERE removed_at IS NULL ORDER BY cached_at DESC"
        ).fetchall()
        removed_rows = conn.execute(
            "SELECT * FROM alert_cache WHERE removed_at IS NOT NULL ORDER BY published_at DESC"
        ).fetchall()
    updated_at = get_meta("last_polled_at")

    def _to_dict(r, include_removed_at=False):
        d = {
            "id":             r["alert_id"],
            "source":         r["source"],
            "title":          r["title_en"],
            "body":           r["body_en"],
            "url":            r["url"],
            "valid_until":    r["valid_until"],
            "service":        r["service"],
            "lines":          json.loads(r["lines"]) if r["lines"] else [],
            "published_at":   r["published_at"],
            "valid_from":     r["valid_from"],
            "severity":       r["severity"],
            "lat":            r["lat"],
            "lon":            r["lon"],
            "location_label": r["location_label"],
            "image":          r["image"],
        }
        if include_removed_at:
            d["removed_at"] = r["removed_at"]
        return d

    alerts = [_to_dict(dict(r)) for r in active_rows]
    removed_alerts = [_to_dict(dict(r), include_removed_at=True) for r in removed_rows]

    source_health_raw = get_meta("source_health")
    source_health = json.loads(source_health_raw) if source_health_raw else {}

    return {"updated_at": updated_at, "alerts": alerts, "removed_alerts": removed_alerts, "source_health": source_health}


# ── meta ─────────────────────────────────────────────────────────────────────

def set_meta(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )


def get_meta(key: str) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


# ── subscribers ───────────────────────────────────────────────────────────────

def add_subscriber(chat_id: int, preferences: Optional[dict] = None) -> bool:
    """Returns True if newly added, False if already exists."""
    prefs = json.dumps(preferences or {"rmv": True, "dwd": True, "polizei": True})
    with _conn() as conn:
        try:
            conn.execute(
                "INSERT INTO subscribers (chat_id, preferences) VALUES (?, ?)",
                (chat_id, prefs),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def remove_subscriber(chat_id: int) -> bool:
    """Hard delete for GDPR /deletedata. Returns True if a row was removed."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0


def get_active_subscribers() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, chat_id, preferences FROM subscribers WHERE active = 1"
        ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["preferences"] = json.loads(r["preferences"])
        result.append(r)
    return result


def deactivate_subscriber(chat_id: int) -> None:
    """Called when Telegram returns 403 Forbidden — user blocked the bot."""
    with _conn() as conn:
        conn.execute("UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,))
