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

# `surge_lead_hours` is the window used to judge "is this soon enough to warn
# about now" — deliberately separate from `interval_hours` (which only drives
# `projected_score`, the next-single-interval estimate). Before this existed,
# the lead-alert check reused `interval_hours` and collapsed the warning
# window into the onset itself (see D-transport-surge-lead-window). 0 disables
# lead-alert detection for categories with no scheduled lookahead.
CATEGORY_WINDOWS: dict[str, dict] = {
    "transport": {"interval_hours": 1, "history_hours": 24, "lookahead_hours": 24, "surge_lead_hours": 3},
    "weather": {"interval_hours": 6, "history_hours": 72, "lookahead_hours": 48, "surge_lead_hours": 12},
    "roadworks": {"interval_hours": 24, "history_hours": 672, "lookahead_hours": 168, "surge_lead_hours": 72},
    "incidents": {"interval_hours": 24, "history_hours": 168, "lookahead_hours": 0, "surge_lead_hours": 0},
    "events": {"interval_hours": 24, "history_hours": 168, "lookahead_hours": 168, "surge_lead_hours": 72},
}

# Extra lookback (beyond a category's own surge_lead_hours) kept clean for the
# lead baseline, so a still-visible scheduled disruption can't absorb itself
# into its own "typical" before it matters — see _build_lead_baseline.
LEAD_BASELINE_POOL_HOURS = 168

# Floor on the moderate band's width, as a fraction of the baseline mean. When
# a category's history is nearly flat (mean ≈ p75), the raw band collapses to
# near-zero width and any real increase jumps straight from minor to severe
# with no moderate step in between (observed 2026-07-05: transport baseline
# mean≈109.86, p75=109.0 — a <1-point band).
MIN_MODERATE_BAND_FRACTION = 0.1

# Bump whenever _compute_weight or any weight table changes. Snapshots are
# stored with this version and baselines only compare same-version scores —
# otherwise every weight calibration poisons the baseline with old-scale
# history for up to the category's full window (4 weeks for roadworks).
WEIGHTS_VERSION = 2

SEVERITY_WEIGHTS_DWD: dict[int, float] = {1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0}
SERVICE_WEIGHTS_RMV: dict[str, float] = {"S-Bahn": 1.5, "U-Bahn": 1.5, "Regional": 1.5, "Tram": 1.0, "Bus": 0.5}
SERVICE_WEIGHTS_BAUSTELLEN: dict[str, float] = {"City (Full)": 1.5, "City (Partial)": 0.5}
WEIGHT_EVENTS = 2.0
WEIGHT_DEFAULT = 1.0

_NO_TEMPORAL_SOURCES = frozenset(("polizei", "strike"))

_STATUS_RANK: dict[str, int] = {"clear": 0, "minor": 1, "moderate": 2, "severe": 3}

# Exclude the trailing N buckets from baseline statistics so an ongoing
# episode doesn't absorb itself into the baseline within hours (July 1:
# p75 jumped 44→131 in ~5h and status flapped severe↔moderate all night).
BASELINE_LAG_BUCKETS = 3


def compute_status(ongoing_score: float, baseline: dict | None, floor: str | None = None) -> str:
    """Return deterministic status label from score and historical baseline.

    `floor` is a minimum status enforced regardless of baseline — used where
    a source provides an authoritative absolute severity (DWD warning levels),
    so e.g. an extreme storm after three calm days can't read "minor" just
    because the baseline is empty.
    """
    if ongoing_score <= 0:
        return "clear"
    if baseline is None:
        status = "minor"
    else:
        # min/max guard: with nonzero-only stats the mean can exceed p75
        # (skewed history), which would make the moderate band empty.
        lo = min(baseline.get("mean", 0), baseline.get("p75", 0))
        hi = max(baseline.get("mean", 0), baseline.get("p75", 0))
        # Floor guard: near-flat history (mean ≈ p75) must still leave a real
        # moderate band, not a sliver a single new alert always jumps past.
        hi = max(hi, lo + baseline.get("mean", 0) * MIN_MODERATE_BAND_FRACTION)
        if ongoing_score <= lo:
            status = "minor"
        elif ongoing_score <= hi:
            status = "moderate"
        else:
            status = "severe"
    if floor and _STATUS_RANK.get(floor, 0) > _STATUS_RANK[status]:
        return floor
    return status


def compute_status_floor(category: str, ongoing_alerts: list[dict]) -> str | None:
    """Absolute status floor from authoritative source severity, if any.

    Only weather has one today: DWD severity 3 (severe) floors the category
    at "moderate", severity 4 (extreme) at "severe". Other categories have no
    authoritative absolute scale — their scores are count- and weight-driven,
    so no floor is derived for them.
    """
    if category != "weather":
        return None
    max_sev = max((a.get("severity") or 0) for a in ongoing_alerts) if ongoing_alerts else 0
    if max_sev >= 4:
        return "severe"
    if max_sev == 3:
        return "moderate"
    return None


def apply_status_hysteresis(
    raw: str, prev_effective: str | None, pending: int, advance: bool,
) -> tuple[str, int]:
    """Damp status de-escalations to stop boundary flapping.

    Escalations (raw >= effective) apply immediately — an alerting system
    must not delay bad news. De-escalations only apply after the raw status
    has been below the effective status for 2 consecutive hourly runs.
    `advance` is False when re-running within the same hour slot (manual
    /pulse trigger), so repeated runs can't burn through the confirmation.

    Returns (effective_status, new_pending_count).
    """
    if prev_effective is None:
        return raw, 0
    if _STATUS_RANK.get(raw, 0) >= _STATUS_RANK.get(prev_effective, 0):
        return raw, 0
    if not advance:
        return prev_effective, pending
    pending += 1
    if pending >= 2:
        return raw, 0
    return prev_effective, pending


def compute_lead_alert(lead_score: float, lead_baseline: dict | None) -> bool:
    """Deterministic replacement for the old LLM-judged "Signal 2".

    True when the load scheduled within `surge_lead_hours` is significant
    relative to what's *typically* scheduled that far ahead. Judged from
    pure future starts — never from expiry schedules of current alerts,
    which is what made the old horizon series fire on RMV end-of-service
    rollovers.

    Deliberately dropped from the original design: a "mostly imminent"
    ratio test (near/total ≥ 50%) against the full lookahead total. It
    structurally couldn't fire for categories like transport where several
    real, independent disruptions are routinely stacked across the
    lookahead window — any single new item is a minority share of that
    total almost by construction (observed 2026-07-05: an incoming S8/S9
    alert was 6 of a 24-point lookahead total, entirely made up of three
    other unrelated future items, not noise). Comparing the lead-window
    score against its *own* history (see _build_lead_baseline) sidesteps
    that: it only asks whether this window's own past shows the arrival
    was unusual, never what fraction of the whole day it represents.

    `lead_baseline` must be built from the lead-window's own score history
    (see _build_lead_baseline), not the ongoing-score baseline — the two
    are different scales, and a single new alert's schedule weight can
    never clear 1.5x a category's much larger total ongoing load.
    """
    if lead_score <= 0:
        return False
    if lead_baseline is None:
        # No typical level to compare against — require a meaningful
        # imminent load on its own.
        return lead_score >= 2.0
    return lead_score >= 1.5 * lead_baseline.get("mean", 0)


def compute_trend(current_score: float, history: list[dict], lead_alert: bool) -> str:
    """Deterministic trend: current score vs. mean of the 3 preceding buckets,
    with a dead band so ±4% noise doesn't flip the label (the LLM used to
    flip stable/worsening on exactly that). A lead alert escalates one step.
    """
    # history includes the current bucket last; compare against the 3 before it
    prior = [h["score"] for h in history[:-1]][-3:]
    if not prior:
        trend = "stable"
    else:
        ref = statistics.mean(prior)
        band = max(0.15 * ref, 1.0)
        if current_score > ref + band:
            trend = "worsening"
        elif current_score < ref - band:
            trend = "improving"
        else:
            trend = "stable"
    if lead_alert:
        trend = {"improving": "stable", "stable": "worsening"}.get(trend, trend)
    return trend


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
            # DB columns kept as-is (upcoming_near_score / scheduled_upcoming_score)
            # to avoid a schema migration; what they represent has changed —
            # see the assignments below and CATEGORY_WINDOWS' surge_lead_hours.
            "upcoming_near_score": 0.0,
            "scheduled_upcoming_score": 0.0,
            "status_floor": None,
        }

    ongoing_by_cat: dict[str, list[dict]] = {cat: [] for cat in CATEGORY_SOURCES}

    near_ends: dict[str, str] = {}
    lead_ends: dict[str, str] = {}
    full_ends: dict[str, str] = {}
    for cat, window in CATEGORY_WINDOWS.items():
        if window["lookahead_hours"] > 0:
            near = now + timedelta(hours=window["interval_hours"])
            full = now + timedelta(hours=window["lookahead_hours"])
            near_ends[cat] = near.strftime("%Y-%m-%dT%H:%M:%SZ")
            full_ends[cat] = full.strftime("%Y-%m-%dT%H:%M:%SZ")
            if window.get("surge_lead_hours", 0) > 0:
                lead = now + timedelta(hours=window["surge_lead_hours"])
                lead_ends[cat] = lead.strftime("%Y-%m-%dT%H:%M:%SZ")

    expiring_near: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    starting_near: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    starting_lead: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}
    starting_full: dict[str, dict] = {cat: {"count": 0, "score": 0.0} for cat in CATEGORY_SOURCES}

    breakdown: dict[str, dict] = {
        cat: {"ongoing": [], "expiring_near": [], "starting_near": [], "starting_lead": [], "starting_full": []}
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
            ongoing_by_cat[cat].append(alert)
            breakdown[cat]["ongoing"].append(entry)
            if cat in near_ends and _is_expiring(alert, now_iso, near_ends[cat]):
                expiring_near[cat]["count"] += 1
                expiring_near[cat]["score"] += weight
                breakdown[cat]["expiring_near"].append(entry)
        elif cat in full_ends and _is_upcoming(alert, now_iso, full_ends[cat]):
            starting_full[cat]["count"] += 1
            starting_full[cat]["score"] += weight
            breakdown[cat]["starting_full"].append(entry)
            if _is_upcoming(alert, now_iso, near_ends[cat]):
                starting_near[cat]["count"] += 1
                starting_near[cat]["score"] += weight
                breakdown[cat]["starting_near"].append(entry)
            if cat in lead_ends and _is_upcoming(alert, now_iso, lead_ends[cat]):
                starting_lead[cat]["count"] += 1
                starting_lead[cat]["score"] += weight
                breakdown[cat]["starting_lead"].append(entry)

    for cat in snapshot:
        snapshot[cat]["ongoing_score"] = round(snapshot[cat]["ongoing_score"], 2)
        projected_count = snapshot[cat]["ongoing_count"] - expiring_near[cat]["count"] + starting_near[cat]["count"]
        projected_score = snapshot[cat]["ongoing_score"] - expiring_near[cat]["score"] + starting_near[cat]["score"]
        snapshot[cat]["projected_count"] = max(0, projected_count)
        snapshot[cat]["projected_score"] = round(max(0.0, projected_score), 2)
        # Lead-window score (this column used to hold the interval-scoped
        # "starting_near" value; it's now scoped to surge_lead_hours instead —
        # see CATEGORY_WINDOWS — since that's the only thing that ever read it,
        # the lead-alert check).
        snapshot[cat]["upcoming_near_score"] = round(starting_lead[cat]["score"], 2)
        # Pure future starts within the full lookahead — the lookahead score.
        snapshot[cat]["scheduled_upcoming_score"] = round(starting_full[cat]["score"], 2)
        snapshot[cat]["status_floor"] = compute_status_floor(cat, ongoing_by_cat[cat])

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
                "lookahead_score": round(r.get("scheduled_upcoming_score", 0.0) or 0.0, 2),
                "lead_score": round(r.get("upcoming_near_score", 0.0) or 0.0, 2),
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
                "lookahead_scores": [],
                "lead_scores": [],
            }
        buckets[bucket_key]["counts"].append(r["ongoing_count"])
        buckets[bucket_key]["scores"].append(r["ongoing_score"])
        buckets[bucket_key]["lookahead_scores"].append(r.get("scheduled_upcoming_score", 0.0) or 0.0)
        buckets[bucket_key]["lead_scores"].append(r.get("upcoming_near_score", 0.0) or 0.0)

    label = "date" if interval_hours >= 24 else "period"
    result = []
    for key in sorted(buckets):
        b = buckets[key]
        entry = {
            label: key[:10] if interval_hours >= 24 else key,
            "count": max(b["counts"]),
            "score": round(max(b["scores"]), 2),
            "lookahead_score": round(max(b["lookahead_scores"]), 2),
            "lead_score": round(max(b["lead_scores"]), 2),
        }
        result.append(entry)
    return result


def _compute_baseline_stats(scores: list[float]) -> dict | None:
    """Mean/p25/p75 baseline from a pool of positive scores, or None if
    there aren't enough samples (< 3) to be meaningful."""
    if len(scores) < 3:
        return None
    sorted_scores = sorted(scores)
    n = len(sorted_scores)
    return {
        "mean": round(statistics.mean(scores), 2),
        "p25": round(sorted_scores[min(int(n * 0.25), n - 1)], 2),
        "p75": round(sorted_scores[min(int(n * 0.75), n - 1)], 2),
        "n": n,
    }


def _build_lead_baseline(
    rows: list[dict], interval_hours: int, surge_lead_hours: int,
) -> dict | None:
    """Baseline for the lead score, built from its own history rather than
    the ongoing-score baseline (the two are different scales — comparing a
    single new alert's schedule weight against the category's total noisy
    ongoing load makes the lead alert structurally unable to fire).

    The lag exclusion is sized to the category's own surge_lead_hours (not a
    flat few buckets): a disruption can sit visible in the lead score for up
    to a full lead window before it starts, so a short exclusion lets it
    absorb itself into its own "typical" before it ever matters.
    """
    buckets = _aggregate_buckets(rows, interval_hours)
    lag_buckets = max(1, -(-surge_lead_hours // max(interval_hours, 1)))  # ceil
    pool = buckets[:-lag_buckets] if len(buckets) > lag_buckets else []
    scores = [b["lead_score"] for b in pool if b.get("lead_score", 0) > 0]
    return _compute_baseline_stats(scores)


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
            current["lookahead"] = {
                "total_score": snap.get("scheduled_upcoming_score", 0.0),
                "lead_score": snap.get("upcoming_near_score", 0.0),
            }

        # Baseline from lagged history: exclude the trailing buckets so an
        # ongoing episode can't normalize itself away within hours.
        baseline_pool = history[:-BASELINE_LAG_BUCKETS] if len(history) > BASELINE_LAG_BUCKETS else []
        baseline = _compute_baseline_stats([h["score"] for h in baseline_pool if h.get("score", 0) > 0])

        lead_baseline = None
        if window.get("surge_lead_hours", 0) > 0:
            lead_since = now - timedelta(hours=window["surge_lead_hours"] + LEAD_BASELINE_POOL_HOURS)
            lead_rows = get_snapshots_fn(cat, lead_since.strftime("%Y-%m-%dT%H:%M:%SZ"))
            lead_baseline = _build_lead_baseline(
                lead_rows, window["interval_hours"], window["surge_lead_hours"],
            )

        ongoing_score = snap.get("ongoing_score", 0.0)
        current["status"] = compute_status(ongoing_score, baseline, snap.get("status_floor"))
        lead_alert = compute_lead_alert(snap.get("upcoming_near_score", 0.0), lead_baseline)
        current["lead_alert"] = lead_alert
        current["trend"] = compute_trend(ongoing_score, history, lead_alert)

        timeseries[cat] = {
            "current": current,
            "history": history,
            "baseline": baseline,
            "window": f"{interval_label} {freq}",
        }

    return timeseries
