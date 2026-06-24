import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from zoneinfo import ZoneInfo

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
    removed_at     TEXT,
    stale          INTEGER NOT NULL DEFAULT 0,
    icon           TEXT
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

CREATE TABLE IF NOT EXISTS quiet_buffer (
    subscriber_id INTEGER NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    alert_id      TEXT NOT NULL,
    buffered_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (subscriber_id, alert_id)
);

CREATE TABLE IF NOT EXISTS pulse_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at   TEXT NOT NULL,
    title          TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL,
    travel_ok      INTEGER NOT NULL DEFAULT 1,
    categories     TEXT NOT NULL DEFAULT '{}',
    avoid          TEXT NOT NULL DEFAULT '[]',
    crowding       TEXT NOT NULL DEFAULT '[]',
    recommendation TEXT NOT NULL DEFAULT '',
    alert_count    INTEGER NOT NULL DEFAULT 0,
    references_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS pulse_daily_summary (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    date         TEXT NOT NULL UNIQUE,
    summary      TEXT NOT NULL,
    generated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    category         TEXT NOT NULL,
    ongoing_count    INTEGER NOT NULL DEFAULT 0,
    ongoing_score    REAL NOT NULL DEFAULT 0.0,
    projected_count  INTEGER NOT NULL DEFAULT 0,
    projected_score  REAL NOT NULL DEFAULT 0.0,
    UNIQUE(timestamp, category)
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
        try:
            conn.execute("ALTER TABLE alert_cache ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE alert_cache ADD COLUMN icon TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE subscribers ADD COLUMN conversation_state TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE subscribers ADD COLUMN last_briefing_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE subscribers ADD COLUMN last_pulse_at TEXT")
        except Exception:
            pass
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS pulse_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_at TEXT NOT NULL,
                summary TEXT NOT NULL,
                travel_ok INTEGER NOT NULL DEFAULT 1,
                categories TEXT NOT NULL DEFAULT '{}',
                avoid TEXT NOT NULL DEFAULT '[]',
                crowding TEXT NOT NULL DEFAULT '[]',
                recommendation TEXT NOT NULL DEFAULT '',
                alert_count INTEGER NOT NULL DEFAULT 0
            )""")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE pulse_history ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        for col in ("avoid", "crowding", "references_json"):
            try:
                conn.execute(f"ALTER TABLE pulse_history ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
            except Exception:
                pass
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS pulse_daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL,
                generated_at TEXT NOT NULL
            )""")
        except Exception:
            pass
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS category_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                category TEXT NOT NULL,
                ongoing_count INTEGER NOT NULL DEFAULT 0,
                ongoing_score REAL NOT NULL DEFAULT 0.0,
                projected_count INTEGER NOT NULL DEFAULT 0,
                projected_score REAL NOT NULL DEFAULT 0.0,
                UNIQUE(timestamp, category)
            )""")
        except Exception:
            pass
        # v0.9.15: rename upcoming_* → projected_* (SQLite can't rename columns)
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(category_snapshots)")]
            if "upcoming_count" in cols and "projected_count" not in cols:
                conn.execute("DROP TABLE category_snapshots")
                conn.execute("""CREATE TABLE category_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    category TEXT NOT NULL,
                    ongoing_count INTEGER NOT NULL DEFAULT 0,
                    ongoing_score REAL NOT NULL DEFAULT 0.0,
                    projected_count INTEGER NOT NULL DEFAULT 0,
                    projected_score REAL NOT NULL DEFAULT 0.0,
                    UNIQUE(timestamp, category)
                )""")
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
            """INSERT INTO processed_alerts (alert_id, source, valid_until) VALUES (?, ?, ?)
               ON CONFLICT(alert_id) DO UPDATE SET valid_until = excluded.valid_until""",
            (alert.id, alert.source, alert.valid_until),
        )


def mark_seen_batch(alerts: list) -> None:
    if not alerts:
        return
    with _conn() as conn:
        conn.executemany(
            """INSERT INTO processed_alerts (alert_id, source, valid_until) VALUES (?, ?, ?)
               ON CONFLICT(alert_id) DO UPDATE SET valid_until = excluded.valid_until""",
            [(a.id, a.source, a.valid_until) for a in alerts],
        )


def expire_processed_alerts() -> None:
    now = datetime.now(timezone.utc)
    exp = (now - timedelta(hours=1)).isoformat()
    ttl = (now - timedelta(days=7)).isoformat()
    pulse_cutoff = (now - timedelta(days=30)).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """DELETE FROM processed_alerts WHERE
               (valid_until IS NOT NULL AND valid_until < ?)
               OR (valid_until IS NULL AND first_seen_at < ?)""",
            (exp, ttl),
        )
        if cur.rowcount:
            log.info("Expired %d processed_alerts entries", cur.rowcount)
        pulse_cur = conn.execute(
            "DELETE FROM pulse_history WHERE generated_at < ?", (pulse_cutoff,)
        )
        if pulse_cur.rowcount:
            log.info("Expired %d pulse_history entries", pulse_cur.rowcount)
        daily_cutoff = (now - timedelta(days=90)).strftime("%Y-%m-%d")
        daily_cur = conn.execute(
            "DELETE FROM pulse_daily_summary WHERE date < ?", (daily_cutoff,)
        )
        if daily_cur.rowcount:
            log.info("Expired %d pulse_daily_summary entries", daily_cur.rowcount)


# ── alert_cache (replaces status.json) ───────────────────────────────────────

def sync_alert_cache(alerts: list, config: dict) -> None:
    """Translate new alerts and sync the cache to match the current fetch result."""
    from translation import translate_alert

    retention_days = config.get('cleared_retention_days', 1)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()

    if not alerts:
        with _conn() as conn:
            conn.execute(
                "UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE removed_at IS NULL"
            )
            conn.execute(
                "DELETE FROM alert_cache WHERE removed_at IS NOT NULL AND removed_at < ?", (cutoff,)
            )
        return

    current_ids = [a.id for a in alerts]
    ph = ",".join("?" * len(current_ids))

    # Determine which alerts already have a cached translation (active only)
    with _conn() as conn:
        cached = {r["alert_id"]: (r["image"], r["stale"], r["valid_until"], r["valid_from"], r["published_at"]) for r in conn.execute(
            f"SELECT alert_id, image, stale, valid_until, valid_from, published_at FROM alert_cache WHERE alert_id IN ({ph}) AND removed_at IS NULL", current_ids
        )}

    # Translate outside the connection — avoids holding a write lock during HTTP calls
    to_insert = []
    to_update_image = []
    to_update_stale = []
    to_update_content = []
    for alert in alerts:
        stale_int = 1 if alert.stale else 0
        if alert.id not in cached:
            en_title, en_body = translate_alert(alert, config)
            to_insert.append((
                alert.id, alert.source, en_title, en_body, alert.url,
                alert.valid_until, alert.service,
                json.dumps(alert.lines) if alert.lines else None,
                alert.published_at, alert.valid_from, alert.severity,
                alert.lat, alert.lon, alert.location_label, alert.image, stale_int,
                alert.icon,
            ))
        else:
            cached_image, cached_stale, cached_valid_until, cached_valid_from, cached_published = cached[alert.id]
            published_changed = (alert.published_at is not None
                                 and cached_published != alert.published_at)
            if (cached_valid_until != alert.valid_until
                    or cached_valid_from != alert.valid_from
                    or published_changed):
                en_title, en_body = translate_alert(alert, config)
                effective_published = alert.published_at if alert.published_at is not None else cached_published
                to_update_content.append((
                    en_title, en_body, alert.url, alert.valid_until,
                    alert.valid_from, effective_published, alert.image, stale_int, alert.icon,
                    alert.id,
                ))
                log.info("alert_cache: updated %s (content changed)", alert.id)
            else:
                if cached_image != alert.image:
                    to_update_image.append((alert.image, alert.id))
                if cached_stale != stale_int:
                    to_update_stale.append((stale_int, alert.id))

    # Batch write: insert new + refresh changed images/stale + remove gone alerts
    with _conn() as conn:
        if to_insert:
            conn.executemany(
                """INSERT OR REPLACE INTO alert_cache
                   (alert_id, source, title_en, body_en, url, valid_until, service,
                    lines, published_at, valid_from, severity, lat, lon, location_label, image, stale, icon, cached_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                           COALESCE(?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                           ?, ?, ?, ?, ?, ?, ?, ?,
                           strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))""",
                to_insert,
            )
        if to_update_image:
            conn.executemany(
                "UPDATE alert_cache SET image = ? WHERE alert_id = ?",
                to_update_image,
            )
        if to_update_content:
            conn.executemany(
                """UPDATE alert_cache SET title_en = ?, body_en = ?, url = ?,
                   valid_until = ?, valid_from = ?, published_at = ?, image = ?, stale = ?, icon = ?,
                   cached_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
                   WHERE alert_id = ?""",
                to_update_content,
            )
            updated_ids = [row[-1] for row in to_update_content]
            uph = ",".join("?" * len(updated_ids))
            conn.execute(
                f"DELETE FROM sent_alerts WHERE alert_id IN ({uph})",
                updated_ids,
            )
        if to_update_stale:
            conn.executemany(
                "UPDATE alert_cache SET stale = ? WHERE alert_id = ?",
                to_update_stale,
            )
        # Mark stale active alerts as removed (keep for rest of day)
        conn.execute(
            f"UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
            f" WHERE alert_id NOT IN ({ph}) AND removed_at IS NULL",
            current_ids
        )
        # Delete removed alerts older than cleared_retention_days
        conn.execute(
            "DELETE FROM alert_cache WHERE removed_at IS NOT NULL AND removed_at < ?", (cutoff,)
        )

    log.info("alert_cache: %d total (%d cached, %d translated, %d updated, %d image updated, %d stale updated)",
             len(alerts), len(cached), len(to_insert), len(to_update_content), len(to_update_image), len(to_update_stale))


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
            "stale":          bool(r["stale"]),
            "icon":           r["icon"],
        }
        if include_removed_at:
            d["removed_at"] = r["removed_at"]
        return d

    alerts = [_to_dict(dict(r)) for r in active_rows]
    removed_alerts = [_to_dict(dict(r), include_removed_at=True) for r in removed_rows]

    source_health_raw = get_meta("source_health")
    source_health = json.loads(source_health_raw) if source_health_raw else {}

    return {"updated_at": updated_at, "alerts": alerts, "removed_alerts": removed_alerts, "source_health": source_health, "pulse": get_latest_pulse()}


# ── Cold-start published_at patch ────────────────────────────────────────────

def patch_published_at() -> None:
    """Correct published_at after a cold start.

    During warm operation the poller sets published_at = now() so the Most Recent
    feed is ordered by when we first saw each alert.  On cold start all alerts in
    the first poll get now(), which loses the real event ordering.  This function
    back-fills a better value for autobahn/baustellen (where valid_from is the
    actual event start) and fixes any NULL rows from sources that never supply one.

    Rules (applied to autobahn, baustellen, dwd, and NULL rows):
    - valid_from in the past  → use valid_from
    - valid_from in the future or absent → use today at 00:00 Frankfurt time
    """
    tz = ZoneInfo("Europe/Berlin")
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    frankfurt_today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_today = frankfurt_today.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with _conn() as conn:
        rows = conn.execute(
            """SELECT alert_id, valid_from FROM alert_cache
               WHERE published_at IS NULL
                  OR (source IN ('autobahn', 'baustellen', 'dwd') AND valid_from IS NOT NULL)"""
        ).fetchall()
        for row in rows:
            if row["valid_from"] and row["valid_from"] < now_iso:
                pub = row["valid_from"]
            else:
                pub = start_of_today
            conn.execute(
                "UPDATE alert_cache SET published_at = ? WHERE alert_id = ?",
                (pub, row["alert_id"]),
            )
    if rows:
        log.info("patch_published_at: fixed %d rows", len(rows))


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
    from notifier.preferences import default_preferences
    prefs = json.dumps(preferences or default_preferences())
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
            "SELECT id, chat_id, preferences, last_briefing_at, last_pulse_at FROM subscribers WHERE active = 1"
        ).fetchall()
    result = []
    for row in rows:
        r = dict(row)
        r["preferences"] = json.loads(r["preferences"])
        result.append(r)
    return result


def update_last_briefing(subscriber_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE subscribers SET last_briefing_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (subscriber_id,),
        )


def update_last_pulse(subscriber_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE subscribers SET last_pulse_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
            (subscriber_id,),
        )


def clear_expired_alerts() -> None:
    """Mark any alert with valid_until in the past as removed."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """UPDATE alert_cache SET removed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               WHERE removed_at IS NULL
                 AND valid_until IS NOT NULL AND valid_until < ?""",
            (now,),
        )
        if cur.rowcount:
            log.info("Cleared %d expired alerts", cur.rowcount)


def get_active_strikes() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT alert_id, valid_from, valid_until, service, title_en, body_en
               FROM alert_cache
               WHERE source = 'strike' AND removed_at IS NULL"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_future_alerts() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM alert_cache
               WHERE removed_at IS NULL
                 AND valid_from IS NOT NULL
                 AND valid_from > strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
               ORDER BY valid_from""",
        ).fetchall()
    return [dict(r) for r in rows]


def get_subscriber_counts() -> dict:
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM subscribers WHERE active = 1").fetchone()[0]
    return {"total": total, "active": active}


def deactivate_subscriber(chat_id: int) -> None:
    """Called when Telegram returns 403 Forbidden — user blocked the bot."""
    with _conn() as conn:
        conn.execute("UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,))


def get_subscriber_by_chat_id(chat_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, chat_id, preferences, active, conversation_state, last_briefing_at FROM subscribers WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row:
        return None
    r = dict(row)
    r["preferences"] = json.loads(r["preferences"])
    if r["conversation_state"]:
        r["conversation_state"] = json.loads(r["conversation_state"])
    return r


def update_subscriber_preferences(chat_id: int, preferences: dict) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE subscribers SET preferences = ? WHERE chat_id = ?",
            (json.dumps(preferences), chat_id),
        )


def set_conversation_state(chat_id: int, state: Optional[dict]) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE subscribers SET conversation_state = ? WHERE chat_id = ?",
            (json.dumps(state) if state else None, chat_id),
        )


def reactivate_subscriber(chat_id: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE subscribers SET active = 1 WHERE chat_id = ?", (chat_id,))


def record_sent_alert(subscriber_id: int, alert_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sent_alerts (subscriber_id, alert_id) VALUES (?, ?)",
            (subscriber_id, alert_id),
        )


def get_unsent_for_subscriber(subscriber_id: int, alert_ids: list[str]) -> list[str]:
    if not alert_ids:
        return []
    ph = ",".join("?" * len(alert_ids))
    with _conn() as conn:
        sent = {r[0] for r in conn.execute(
            f"SELECT alert_id FROM sent_alerts WHERE subscriber_id = ? AND alert_id IN ({ph})",
            [subscriber_id] + alert_ids,
        )}
    return [aid for aid in alert_ids if aid not in sent]


def buffer_quiet_alert(subscriber_id: int, alert_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO quiet_buffer (subscriber_id, alert_id) VALUES (?, ?)",
            (subscriber_id, alert_id),
        )


def flush_quiet_buffer(subscriber_id: int) -> list[tuple[str, str]]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT alert_id, buffered_at FROM quiet_buffer WHERE subscriber_id = ? ORDER BY buffered_at",
            (subscriber_id,),
        ).fetchall()
        if rows:
            conn.execute(
                "DELETE FROM quiet_buffer WHERE subscriber_id = ?",
                (subscriber_id,),
            )
    return [(r[0], r[1]) for r in rows]


# ── alert_cache queries (notifier) ──────────────────────────────────────────

def get_alerts_since(since_ts: Optional[str]) -> list[dict]:
    """Return active, non-stale alert_cache rows cached after *since_ts*."""
    with _conn() as conn:
        if since_ts:
            rows = conn.execute(
                "SELECT * FROM alert_cache WHERE cached_at > ? AND removed_at IS NULL AND stale = 0 ORDER BY cached_at",
                (since_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alert_cache WHERE removed_at IS NULL AND stale = 0 ORDER BY cached_at"
            ).fetchall()
    return [dict(r) for r in rows]


def get_all_active_alerts() -> list[dict]:
    """Return all active (non-removed) alert_cache rows for daily summary."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alert_cache WHERE removed_at IS NULL ORDER BY cached_at"
        ).fetchall()
    return [dict(r) for r in rows]


def store_pulse(pulse: dict) -> None:
    hour_prefix = pulse["generated_at"][:13]
    with _conn() as conn:
        conn.execute(
            "DELETE FROM pulse_history WHERE generated_at LIKE ?",
            (hour_prefix + "%",),
        )
        conn.execute(
            """INSERT INTO pulse_history
               (generated_at, title, summary, travel_ok, categories, avoid, crowding, recommendation, alert_count, references_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pulse["generated_at"],
                pulse.get("title", ""),
                pulse["summary"],
                1,
                json.dumps(pulse.get("categories", {})),
                json.dumps(pulse.get("avoid", [])),
                json.dumps(pulse.get("crowding", [])),
                pulse.get("recommendation", ""),
                pulse.get("alert_count", 0),
                json.dumps(pulse.get("references", [])),
            ),
        )


def _parse_pulse_row(row) -> dict:
    d = dict(row)
    d.pop("travel_ok", None)
    d["categories"] = json.loads(d["categories"])
    d["avoid"] = json.loads(d.get("avoid") or "[]")
    d["crowding"] = json.loads(d.get("crowding") or "[]")
    d["references"] = json.loads(d.get("references_json") or "[]")
    d.pop("references_json", None)
    return d


def get_latest_pulse() -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM pulse_history ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return _parse_pulse_row(row)


def get_recent_pulses(limit: int = 3) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pulse_history ORDER BY generated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_parse_pulse_row(row) for row in rows]


def get_pulses_since(since: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pulse_history WHERE generated_at >= ? ORDER BY generated_at",
            (since,),
        ).fetchall()
    return [_parse_pulse_row(row) for row in rows]


def store_daily_summary(date: str, summary: str, generated_at: str) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO pulse_daily_summary (date, summary, generated_at)
               VALUES (?, ?, ?)""",
            (date, summary, generated_at),
        )


def get_recent_daily_summaries(limit: int = 3) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pulse_daily_summary ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_pulses_for_date(date: str) -> list[dict]:
    start = f"{date}T00:00:00Z"
    end = f"{date}T23:59:59Z"
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM pulse_history WHERE generated_at >= ? AND generated_at <= ? ORDER BY generated_at",
            (start, end),
        ).fetchall()
    return [_parse_pulse_row(row) for row in rows]


def store_category_snapshots(timestamp: str, snapshots: dict) -> None:
    with _conn() as conn:
        for category, data in snapshots.items():
            conn.execute(
                """INSERT OR REPLACE INTO category_snapshots
                   (timestamp, category, ongoing_count, ongoing_score, projected_count, projected_score)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    timestamp,
                    category,
                    data.get("ongoing_count", 0),
                    data.get("ongoing_score", 0.0),
                    data.get("projected_count", 0),
                    data.get("projected_score", 0.0),
                ),
            )


def get_category_snapshots(category: str, since: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM category_snapshots WHERE category = ? AND timestamp >= ? ORDER BY timestamp",
            (category, since),
        ).fetchall()
    return [dict(r) for r in rows]


def search_active_alerts(query: str) -> list[dict]:
    """Token-based search across active alerts — matches title_en, body_en, service, location_label."""
    tokens = query.lower().split()
    if not tokens:
        return []
    all_alerts = get_all_active_alerts()
    results = []
    for row in all_alerts:
        hay = " ".join(
            (row.get("title_en") or "", row.get("body_en") or "",
             row.get("service") or "", row.get("location_label") or "")
        ).lower()
        if all(t in hay for t in tokens):
            results.append(row)
    sev_order = {4: 0, 3: 1, 2: 2, 1: 3}
    results.sort(key=lambda r: (sev_order.get(r.get("severity") or 0, 4), r.get("cached_at") or ""), reverse=False)
    return results
