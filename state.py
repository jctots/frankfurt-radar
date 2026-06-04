import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import Alert

from translation import translate_alert

log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "."))
SEEN_FILE = DATA_DIR / "seen.json"
STATUS_FILE = DATA_DIR / "status.json"


def load_seen() -> dict:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def expire_seen(seen: dict) -> dict:
    now = datetime.now(timezone.utc)
    expiry_cutoff = now - timedelta(hours=1)
    ttl_cutoff = now - timedelta(days=7)
    active = {}
    for alert_id, entry in seen.items():
        valid_until = entry.get("valid_until")
        if valid_until:
            try:
                dt = datetime.fromisoformat(valid_until)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt > expiry_cutoff:
                    active[alert_id] = entry
            except ValueError:
                active[alert_id] = entry
        else:
            # No expiry date (e.g. police items) — expire after 7 days via notified_at
            notified_raw = entry.get("notified_at")
            if notified_raw:
                try:
                    notified = datetime.fromisoformat(notified_raw)
                    if notified.tzinfo is None:
                        notified = notified.replace(tzinfo=timezone.utc)
                    if notified > ttl_cutoff:
                        active[alert_id] = entry
                except ValueError:
                    active[alert_id] = entry
            else:
                active[alert_id] = entry
    removed = len(seen) - len(active)
    if removed:
        log.info("Expired %d seen entries", removed)
    return active


def write_status(alerts: list["Alert"], config: dict) -> None:
    # Reuse cached translations from the previous status.json to avoid re-translating seen alerts
    cached: dict[str, dict] = {}
    if STATUS_FILE.exists():
        try:
            prev = json.loads(STATUS_FILE.read_text())
            cached = {a["id"]: a for a in prev.get("alerts", [])}
        except Exception:
            pass

    entries = []
    for alert in alerts:
        if alert.id in cached:
            entries.append(cached[alert.id])
        else:
            en_title, en_body = translate_alert(alert, config)
            entries.append({
                "id": alert.id,
                "source": alert.source,
                "title": en_title,
                "body": en_body,
                "url": alert.url,
                "valid_until": alert.valid_until,
                "service": alert.service,
                "lines": alert.lines,
                "published_at": alert.published_at,
                "severity": alert.severity,
                "lat": alert.lat,
                "lon": alert.lon,
                "location_label": alert.location_label,
            })

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "alerts": entries,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    cached_count = sum(1 for e in entries if e["id"] in cached)
    log.info("status.json: %d alerts (%d cached, %d translated)", len(entries), cached_count, len(entries) - cached_count)
