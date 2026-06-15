import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json

import psutil
import yaml
from dotenv import load_dotenv

from db import expire_processed_alerts, get_meta, init_db, set_meta, sync_alert_cache
from notifications import notify_admin_health
from pipeline import process_alerts
from pollers import AutobahnPoller, BaustellenPoller, DWDPoller, OpenLigaPoller, PolizeiPoller, RMVPoller, StaticEventsPoller, StaticSportsPoller, TicketmasterPoller
from translation import reset_translation_health, translation_ok

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CONFIG_FILE  = Path(os.getenv("DATA_DIR", "data")) / "config.yaml"
EVENTS_FILE  = Path(os.getenv("DATA_DIR", "data")) / "city_events.yaml"
SPORTS_FILE  = Path(os.getenv("DATA_DIR", "data")) / "sports_events.yaml"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        log.error("config.yaml not found")
        sys.exit(1)
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)


def load_city_events() -> list:
    if not EVENTS_FILE.exists():
        log.warning("city_events.yaml not found — no city events will be shown")
        return []
    with EVENTS_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def load_sports_events() -> list:
    if not SPORTS_FILE.exists():
        log.warning("sports_events.yaml not found — no sports events will be shown")
        return []
    with SPORTS_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _system_metrics() -> dict:
    cpu_pct = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    try:
        load1, load5, _ = psutil.getloadavg()
    except AttributeError:
        load1, load5 = 0.0, 0.0
    return {
        "cpu_pct": cpu_pct,
        "ram_pct": mem.percent,
        "ram_used_gb": mem.used / 1024 ** 3,
        "ram_total_gb": mem.total / 1024 ** 3,
        "swap_used_mb": swap.used / 1024 ** 2,
        "load1": load1,
        "load5": load5,
        "cpu_count": psutil.cpu_count() or 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="frankfurt-radar poller")
    parser.add_argument(
        "--mode",
        choices=["poll", "daily"],
        default="poll",
        help="poll: new disruptions only; daily: morning summary grouped by service",
    )
    args = parser.parse_args()

    api_key = os.getenv("RMV_API_KEY", "")
    if not api_key:
        log.error("RMV_API_KEY not set in environment")
        sys.exit(1)

    config = load_config()
    init_db()

    # Check poll staleness before this run (detects missed/delayed cron)
    health_cfg = config.get("admin_health_notifier") or {}
    stale_minutes = health_cfg.get("poll_stale_minutes", 0)
    poll_fresh = True
    if stale_minutes:
        last_polled = get_meta("last_polled_at")
        if last_polled:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last_polled)
                poll_fresh = age <= timedelta(minutes=stale_minutes)
            except ValueError:
                pass

    reset_translation_health()

    transport_cfg = config.get("transport", {})
    services = transport_cfg.get("services") or {}

    pollers = []
    if transport_cfg.get("enabled", True):
        pollers.append(RMVPoller(api_key=api_key, services=services))
    if config.get("police", {}).get("enabled", False):
        pollers.append(PolizeiPoller())
    if config.get("weather", {}).get("enabled", False):
        pollers.append(DWDPoller(min_severity=config["weather"].get("min_severity", 1)))
    radius_km = float(config.get("location", {}).get("radius_km", 50.0))
    autobahn_cfg = config.get("autobahn", {})
    if autobahn_cfg.get("enabled", False):
        pollers.append(AutobahnPoller(
            roads=autobahn_cfg.get("roads") or None,
            radius_km=radius_km,
            kinds=autobahn_cfg.get("kinds") or None,
        ))
    baustellen_cfg = config.get("baustellen", {})
    if baustellen_cfg.get("enabled", False):
        closures_cfg = baustellen_cfg.get("closures", ["full", "partial"])
        sperrung_filter: set[int] = set()
        if "full" in closures_cfg:
            sperrung_filter.add(1)
        if "partial" in closures_cfg:
            sperrung_filter.add(0)
        pollers.append(BaustellenPoller(sperrung_filter=sperrung_filter or None))
    events_cfg = config.get("events", {})
    if events_cfg.get("enabled", False):
        pollers.append(StaticEventsPoller(
            events=load_city_events(),
            advance_days=events_cfg.get("advance_days", 7),
        ))
    sports_cfg = config.get("sports", {})
    if sports_cfg.get("enabled", False):
        advance_days = sports_cfg.get("advance_days", 3)
        pollers.append(StaticSportsPoller(
            events=load_sports_events(),
            advance_days=advance_days,
        ))
        # Network sports pollers run at most once per day
        last_sports = get_meta("last_sports_polled_at")
        sports_due = True
        if last_sports:
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(last_sports)
                sports_due = age >= timedelta(hours=24)
            except ValueError:
                pass
        if sports_due:
            pollers.append(OpenLigaPoller(advance_days=advance_days))
            tm_api_key = os.getenv("TICKETMASTER_API_KEY", "")
            if tm_api_key:
                pollers.append(TicketmasterPoller(api_key=tm_api_key))
            else:
                log.warning("TICKETMASTER_API_KEY not set — Ticketmaster poller disabled")
            set_meta("last_sports_polled_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        else:
            log.info("Sports network pollers skipped — last run %s", last_sports)

    fetched = [(p, p.fetch()) for p in pollers]
    all_alerts = [a for _, alerts in fetched for a in alerts]

    # Collect per-source health; static pollers (no network) are always ok.
    source_health = {type(p).__name__: p.ok for p, _ in fetched}
    set_meta("source_health", json.dumps(source_health))

    max_age = config.get("police", {}).get("max_age_hours", 48)
    if max_age:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age)).isoformat()
        all_alerts = [
            a for a in all_alerts
            if not (a.source == "polizei" and a.published_at and a.published_at < cutoff)
        ]

    stale_after_days = config.get("stale_after_days")
    if stale_after_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_after_days)).isoformat()
        for a in all_alerts:
            if a.source not in ("rmv", "autobahn", "baustellen"):
                continue
            # autobahn/baustellen set published_at=now() for feed ordering; use valid_from
            # as the true event age for stale classification
            age_ref = (a.valid_from if a.source in ("autobahn", "baustellen") and a.valid_from else a.published_at)
            if age_ref and age_ref < cutoff:
                a.stale = True

    sync_alert_cache(all_alerts, config)
    expire_processed_alerts()
    process_alerts(all_alerts, mode=args.mode, config=config)
    set_meta("last_polled_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    if health_cfg:
        metrics = _system_metrics()
        ram_warn_pct = health_cfg.get("ram_warn_pct", 85)

        current_health: dict[str, bool] = {type(p).__name__: p.ok for p, _ in fetched}
        current_health["translator"] = translation_ok()
        if stale_minutes:
            current_health["poll_schedule"] = poll_fresh
        current_health["ram"] = metrics["ram_pct"] <= ram_warn_pct
        current_health["load"] = metrics["load1"] <= metrics["cpu_count"]

        prev_raw = get_meta("admin_health")
        prev_health: dict[str, bool] = json.loads(prev_raw) if prev_raw else {}

        _display = {
            "translator": "Translator",
            "poll_schedule": "Cron schedule",
            "ram": f"RAM ({metrics['ram_pct']:.0f}%)",
            "load": f"Load ({metrics['load1']:.1f}/{metrics['cpu_count']})",
        }

        def _fmt(keys: list[str]) -> str:
            return ", ".join(_display.get(k, k.replace("Poller", "")) for k in keys)

        metrics_line = (
            f"CPU: {metrics['cpu_pct']:.0f}% · "
            f"Load: {metrics['load1']:.1f}/{metrics['cpu_count']} · "
            f"RAM: {metrics['ram_used_gb']:.1f}/{metrics['ram_total_gb']:.1f} GB ({metrics['ram_pct']:.0f}%) · "
            f"Swap: {metrics['swap_used_mb']:.0f} MB"
        )

        degraded = [k for k, ok in current_health.items() if not ok and prev_health.get(k, True)]
        recovered = [k for k, ok in current_health.items() if ok and k in prev_health and not prev_health[k]]

        if degraded:
            notify_admin_health("🔴 Frankfurt Radar — health alert", f"Failing: {_fmt(degraded)}\n\n{metrics_line}", config)
        if recovered:
            notify_admin_health("🟢 Frankfurt Radar — recovered", f"Recovered: {_fmt(recovered)}\n\n{metrics_line}", config)

        set_meta("admin_health", json.dumps(current_health))


if __name__ == "__main__":
    main()
