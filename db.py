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
    severity       INTEGER,
    lat            REAL,
    lon            REAL,
    location_label TEXT,
    cached_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
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
            conn.execute("DELETE FROM alert_cache")
        return

    current_ids = [a.id for a in alerts]
    ph = ",".join("?" * len(current_ids))

    # Determine which alerts already have a cached translation (read-only, short)
    with _conn() as conn:
        cached = {r[0] for r in conn.execute(
            f"SELECT alert_id FROM alert_cache WHERE alert_id IN ({ph})", current_ids
        )}

    # Translate outside the connection — avoids holding a write lock during HTTP calls
    to_insert = []
    for alert in alerts:
        if alert.id not in cached:
            en_title, en_body = translate_alert(alert, config)
            to_insert.append((
                alert.id, alert.source, en_title, en_body, alert.url,
                alert.valid_until, alert.service,
                json.dumps(alert.lines) if alert.lines else None,
                alert.published_at, alert.severity,
                alert.lat, alert.lon, alert.location_label,
            ))

    # Batch write: insert new + remove alerts no longer in the current fetch
    with _conn() as conn:
        if to_insert:
            conn.executemany(
                """INSERT OR REPLACE INTO alert_cache
                   (alert_id, source, title_en, body_en, url, valid_until, service,
                    lines, published_at, severity, lat, lon, location_label, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                to_insert,
            )
        conn.execute(
            f"DELETE FROM alert_cache WHERE alert_id NOT IN ({ph})", current_ids
        )

    log.info("alert_cache: %d total (%d cached, %d translated)",
             len(alerts), len(cached), len(to_insert))


def get_status_json() -> dict:
    """Return {updated_at, alerts: [...]} matching the former status.json schema."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_cache ORDER BY cached_at DESC"
        ).fetchall()
        updated_at_row = conn.execute(
            "SELECT MAX(cached_at) FROM alert_cache"
        ).fetchone()

    updated_at = updated_at_row[0] if updated_at_row else None
    alerts = []
    for row in rows:
        r = dict(row)
        alerts.append({
            "id":             r["alert_id"],
            "source":         r["source"],
            "title":          r["title_en"],
            "body":           r["body_en"],
            "url":            r["url"],
            "valid_until":    r["valid_until"],
            "service":        r["service"],
            "lines":          json.loads(r["lines"]) if r["lines"] else [],
            "published_at":   r["published_at"],
            "severity":       r["severity"],
            "lat":            r["lat"],
            "lon":            r["lon"],
            "location_label": r["location_label"],
        })

    return {"updated_at": updated_at, "alerts": alerts}


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
