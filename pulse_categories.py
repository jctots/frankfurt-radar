"""Deterministic snapshot computation and time-series aggregation for City Pulse.

Hourly snapshots capture per-category ongoing and upcoming alert counts with
severity-weighted scores. Time-series are aggregated per category's natural
sample interval and fed to the LLM, which judges status and trend.

Source-to-category mapping:
  weather    = dwd
  transport  = rmv
  roadworks  = autobahn, baustellen
  incidents  = polizei, strike
  events     = events, sports, messe
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Callable

CATEGORY_SOURCES: dict[str, list[str]] = {
    "weather": ["dwd"],
    "transport": ["rmv"],
    "roadworks": ["autobahn", "baustellen"],
    "incidents": ["polizei", "strike", "feuerwehr"],
    "events": ["events", "sports", "messe"],
}

CATEGORY_STATUS_LABELS: dict[str, list[str]] = {
    "transport": ["clear", "delays", "disrupted", "paralyzed"],
    "weather": ["clear", "watch", "warning", "extreme"],
    "roadworks": ["clear", "works", "closures", "gridlock"],
    "incidents": ["clear", "low", "elevated", "major"],
    "events": ["clear", "crowds", "busy", "peak"],
}

CATEGORY_WINDOWS: dict[str, dict] = {
    "transport": {"interval_hours": 1, "history_hours": 24, "lookahead_hours": 6},
    "weather": {"interval_hours": 6, "history_hours": 72, "lookahead_hours": 48},
    "roadworks": {"interval_hours": 24, "history_hours": 672, "lookahead_hours": 168},
    "incidents": {"interval_hours": 24, "history_hours": 168, "lookahead_hours": 0},
    "events": {"interval_hours": 24, "history_hours": 168, "lookahead_hours": 168},
}

SEVERITY_WEIGHTS_DWD: dict[int, float] = {1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0}
SERVICE_WEIGHTS_RMV: dict[str, float] = {"S-Bahn": 1.5, "U-Bahn": 1.5, "Regional": 1.5, "Tram": 1.0, "Bus": 0.5}
SERVICE_WEIGHTS_BAUSTELLEN: dict[str, float] = {"City (Full)": 1.5, "City (Partial)": 0.5}
WEIGHT_EVENTS = 2.0
WEIGHT_DEFAULT = 1.0

_NO_TEMPORAL_SOURCES = frozenset(("polizei", "strike"))


def compute_status(ongoing_score: float, baseline: dict | None) -> str:
    """Return deterministic status label from score and historical baseline."""
    if ongoing_score <= 0:
        return "clear"
    if baseline is None:
        return "minor"
    mean = baseline.get("mean", 0)
    p75 = baseline.get("p75", 0)
    if ongoing_score <= mean:
        return "minor"
    if ongoing_score <= p75:
        return "moderate"
    return "severe"


def _compute_weight(alert: dict) -> float:
    source = alert.get("source", "")
    if source == "dwd":
        return SEVERITY_WEIGHTS_DWD.get(alert.get("severity"), WEIGHT_DEFAULT)
    if source == "rmv":
        base = SERVICE_WEIGHTS_RMV.get(alert.get("service"), WEIGHT_DEFAULT)
        line_count = len(alert.get("lines") or [])
        return base * max(1, min(line_count, 4))
    if source == "autobahn":
        title = (alert.get("title_en") or alert.get("title") or "").lower()
        return 2.0 if "closure" in title else WEIGHT_DEFAULT
    if source == "baustellen":
        return SERVICE_WEIGHTS_BAUSTELLEN.get(alert.get("service"), WEIGHT_DEFAULT)
    if source in ("events", "sports", "messe"):
        return WEIGHT_EVENTS
    if source == "strike":
        return 1.5
    if source == "polizei":
        return 0.5
    if source == "feuerwehr":
        return 1.0
    return WEIGHT_DEFAULT


def _is_ongoing(alert: dict, now_iso: str) -> bool:
    source = alert.get("source", "")
    if source in _NO_TEMPORAL_SOURCES:
        return True
    valid_from = alert.get("valid_from")
    valid_until = alert.get("valid_until")
    if valid_from and valid_from > now_iso:
        return False
    if valid_until and valid_until < now_iso:
        return False
    return True


def _is_upcoming(alert: dict, now_iso: str, lookahead_end_iso: str) -> bool:
    source = alert.get("source", "")
    if source in _NO_TEMPORAL_SOURCES:
        return False
    valid_from = alert.get("valid_from")
    if not valid_from:
        return False
    return valid_from > now_iso and valid_from <= lookahead_end_iso


def _is_expiring(alert: dict, now_iso: str, lookahead_end_iso: str) -> bool:
    source = alert.get("source", "")
    if source in _NO_TEMPORAL_SOURCES:
        return False
    valid_until = alert.get("valid_until")
    if not valid_until:
        return False
    return valid_until > now_iso and valid_until <= lookahead_end_iso


def count_alerts_by_category(
    alerts: list[dict], now: datetime | None = None,
) -> dict[str, float]:
    source_to_cat = {}
    for cat, sources in CATEGORY_SOURCES.items():
        for src in sources:
            source_to_cat[src] = cat

    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    counts: dict[str, float] = {cat: 0.0 for cat in CATEGORY_SOURCES}
    for alert in alerts:
        if alert.get("stale"):
            continue
        if not _is_ongoing(alert, now_iso):
            continue
        cat = source_to_cat.get(alert.get("source", ""))
        if cat:
            counts[cat] += _compute_weight(alert)
    return counts


def compute_snapshot(
    alerts: list[dict], now: datetime | None = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """Return (snapshot, score_breakdown) for all categories.

    snapshot: per-category scores for DB storage and timeseries.
    score_breakdown: per-category lists of contributing alerts by bucket,
        for debug logging only.
    """
    source_to_cat = {}
    for cat, sources in CATEGORY_SOURCES.items():
        for src in sources:
            source_to_cat[src] = cat

    if now is None:
        now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot: dict[str, dict] = {}
    for cat in CATEGORY_SOURCES:
        snapshot[cat] = {
            "ongoing_count": 0,
            "ongoing_score": 0.0,
            "projected_count": 0,
            "projected_score": 0.0,
            "upcoming_count": 0,
            "upcoming_score": 0.0,
            "upcoming_near_score": 0.0,
        }

    near_ends: dict[str, str] = {}
    full_ends: dict[str, str] = {}
    for cat, window in CATEGORY_WINDOWS.items():
        if window["lookahead_hours"] > 0:
            near = now + timedelta(hours=window["interval_hours"])
            full = now + timedelta(hours=window["lookahead_hours"])
            near_ends[cat] = near.strftime("%Y-%m-%dT%H:%M:%SZ")
            full_ends[cat] = full.strftime("%Y-%m-%dT%H:%M:%SZ")

    expiring_near: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    expiring_full: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    starting_near: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    starting_full: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}

    breakdown: dict[str, dict] = {
        cat: {"ongoing": [], "expiring_near": [], "starting_near": [], "starting_full": []}
        for cat in CATEGORY_SOURCES
    }

    def _alert_entry(alert: dict, weight: float) -> dict:
        return {
            "alert_id": alert.get("alert_id"),
            "source": alert.get("source"),
            "weight": weight,
            "title": (alert.get("title_en") or "")[:80],
            "body": (alert.get("body_en") or "")[:100],
        }

    for alert in alerts:
        if alert.get("stale"):
            continue
        cat = source_to_cat.get(alert.get("source", ""))
        if not cat:
            continue
        weight = _compute_weight(alert)
        entry = _alert_entry(alert, weight)
        if _is_ongoing(alert, now_iso):
            snapshot[cat]["ongoing_count"] += 1
            snapshot[cat]["ongoing_score"] += weight
            breakdown[cat]["ongoing"].append(entry)
            if cat in near_ends and _is_expiring(alert, now_iso, near_ends[cat]):
                expiring_near[cat]["count"] += 1
                expiring_near[cat]["score"] += weight
                breakdown[cat]["expiring_near"].append(entry)
            if cat in full_ends and _is_expiring(alert, now_iso, full_ends[cat]):
                expiring_full[cat]["count"] += 1
                expiring_full[cat]["score"] += weight
        elif cat in full_ends and _is_upcoming(alert, now_iso, full_ends[cat]):
            starting_full[cat]["count"] += 1
            starting_full[cat]["score"] += weight
            breakdown[cat]["starting_full"].append(entry)
            if _is_upcoming(alert, now_iso, near_ends[cat]):
                starting_near[cat]["count"] += 1
                starting_near[cat]["score"] += weight
                breakdown[cat]["starting_near"].append(entry)

    for cat in snapshot:
        snapshot[cat]["ongoing_score"] = round(snapshot[cat]["ongoing_score"], 2)
        projected_count = snapshot[cat]["ongoing_count"] - expiring_near[cat]["count"] + starting_near[cat]["count"]
        projected_score = snapshot[cat]["ongoing_score"] - expiring_near[cat]["score"] + starting_near[cat]["score"]
        snapshot[cat]["projected_count"] = max(0, projected_count)
        snapshot[cat]["projected_score"] = round(max(0.0, projected_score), 2)
        horizon_count = snapshot[cat]["ongoing_count"] - expiring_full[cat]["count"] + starting_full[cat]["count"]
        horizon_score = snapshot[cat]["ongoing_score"] - expiring_full[cat]["score"] + starting_full[cat]["score"]
        snapshot[cat]["upcoming_count"] = max(0, horizon_count)
        snapshot[cat]["upcoming_score"] = round(max(0.0, horizon_score), 2)
        snapshot[cat]["upcoming_near_score"] = round(starting_near[cat]["score"], 2)

    return snapshot, breakdown


def _aggregate_buckets(
    rows: list[dict], interval_hours: int,
) -> list[dict]:
    if interval_hours <= 1:
        return [
            {
                "hour": r["timestamp"],
                "count": r["ongoing_count"],
                "score": r["ongoing_score"],
                "horizon_score": round(r.get("upcoming_score", 0.0), 2),
            }
            for r in rows
        ]

    buckets: dict[str, dict] = {}
    for r in rows:
        ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
        bucket_hour = (ts.hour // interval_hours) * interval_hours
        bucket_ts = ts.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        bucket_key = bucket_ts.strftime("%Y-%m-%dT%H:%M:%SZ")

        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "counts": [],
                "scores": [],
                "upcoming_scores": [],
            }
        buckets[bucket_key]["counts"].append(r["ongoing_count"])
        buckets[bucket_key]["scores"].append(r["ongoing_score"])
        buckets[bucket_key]["upcoming_scores"].append(r.get("upcoming_score", 0.0))

    label = "date" if interval_hours >= 24 else "period"
    result = []
    for key in sorted(buckets):
        b = buckets[key]
        entry = {
            label: key[:10] if interval_hours >= 24 else key,
            "count": max(b["counts"]),
            "score": round(max(b["scores"]), 2),
            "horizon_score": round(max(b["upcoming_scores"]), 2),
        }
        result.append(entry)
    return result


def build_category_timeseries(
    get_snapshots_fn: Callable[[str, str], list[dict]],
    current_snapshot: dict[str, dict],
    now: datetime | None = None,
) -> dict:
    if now is None:
        now = datetime.now(timezone.utc)

    timeseries: dict[str, dict] = {}
    for cat, window in CATEGORY_WINDOWS.items():
        since = now - timedelta(hours=window["history_hours"])
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")

        rows = get_snapshots_fn(cat, since_iso)
        history = _aggregate_buckets(rows, window["interval_hours"])

        interval_label = f"{window['history_hours']}h"
        if window["interval_hours"] >= 24:
            days = window["history_hours"] // 24
            weeks = days // 7
            interval_label = f"{weeks}w" if weeks > 0 else f"{days}d"
        freq = "hourly" if window["interval_hours"] == 1 else (
            f"{window['interval_hours']}h" if window["interval_hours"] < 24 else "daily"
        )

        snap = current_snapshot.get(cat, {})
        current: dict = {
            "ongoing": {
                "count": snap.get("ongoing_count", 0),
                "score": snap.get("ongoing_score", 0.0),
            },
            "projected": {
                "count": snap.get("projected_count", 0),
                "score": snap.get("projected_score", 0.0),
            },
        }

        if window["lookahead_hours"] > 0:
            current["horizon"] = {
                "total_score": snap.get("upcoming_score", 0.0),
                "near_score": snap.get("upcoming_near_score", 0.0),
            }

        scores = [h["score"] for h in history if h.get("score", 0) > 0]
        if len(scores) >= 3:
            sorted_scores = sorted(scores)
            n = len(sorted_scores)
            baseline = {
                "mean": round(statistics.mean(scores), 2),
                "p25": round(sorted_scores[min(int(n * 0.25), n - 1)], 2),
                "p75": round(sorted_scores[min(int(n * 0.75), n - 1)], 2),
                "n": n,
            }
        else:
            baseline = None

        current["status"] = compute_status(snap.get("ongoing_score", 0.0), baseline)

        timeseries[cat] = {
            "current": current,
            "history": history,
            "baseline": baseline,
            "window": f"{interval_label} {freq}",
        }

    return timeseries
