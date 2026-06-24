"""Deterministic category status and trend computation for City Pulse.

Categories are computed from active alert counts, not by the LLM. This gives
consistent, explainable results and handles cold-start gracefully.

Only ongoing alerts count toward the trend — future alerts (valid_from > now)
and expired alerts (valid_until < now) are excluded. Sources without temporal
data (polizei, strike RSS feeds) are always counted.

Each alert is severity-weighted rather than counted as 1. Weights are
derived deterministically from existing alert fields (severity, service,
title keywords). See _compute_weight() for the mapping.

Status levels (clear → low → moderate → high) compare the current weighted
count against the EWMA using sigma bands: σ = max(EWMA × 0.15, 1.0).

Trends are decoupled from status. They measure the EWMA slope — whether the
baseline itself is rising or falling — by comparing the current EWMA against
the previous EWMA value.

Source-to-category mapping:
  weather    = dwd
  transport  = rmv
  roadworks  = autobahn, baustellen
  incidents  = polizei, strike
  events     = events, sports, messe
"""

from __future__ import annotations

from datetime import datetime, timezone

CATEGORY_SOURCES: dict[str, list[str]] = {
    "weather": ["dwd"],
    "transport": ["rmv"],
    "roadworks": ["autobahn", "baustellen"],
    "incidents": ["polizei", "strike"],
    "events": ["events", "sports", "messe"],
}

STATUS_LEVELS = ("clear", "low", "moderate", "high")

EWMA_ALPHA = 0.3

SEVERITY_WEIGHTS_DWD: dict[int, float] = {1: 0.5, 2: 1.0, 3: 1.5, 4: 2.0}
SERVICE_WEIGHTS_RMV: dict[str, float] = {"S-Bahn": 1.5, "U-Bahn": 1.5, "Regional": 1.5}
SERVICE_WEIGHTS_BAUSTELLEN: dict[str, float] = {"City (Full)": 1.5}
WEIGHT_EVENTS = 2.0
WEIGHT_DEFAULT = 1.0

_NO_TEMPORAL_SOURCES = frozenset(("polizei", "strike"))


def _compute_weight(alert: dict) -> float:
    source = alert.get("source", "")
    if source == "dwd":
        return SEVERITY_WEIGHTS_DWD.get(alert.get("severity"), WEIGHT_DEFAULT)
    if source == "rmv":
        return SERVICE_WEIGHTS_RMV.get(alert.get("service"), WEIGHT_DEFAULT)
    if source == "autobahn":
        title = (alert.get("title_en") or alert.get("title") or "").lower()
        return 1.5 if "closure" in title else WEIGHT_DEFAULT
    if source == "baustellen":
        return SERVICE_WEIGHTS_BAUSTELLEN.get(alert.get("service"), WEIGHT_DEFAULT)
    if source in ("events", "sports", "messe"):
        return WEIGHT_EVENTS
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


def compute_ewma(pulses: list[dict], alpha: float = EWMA_ALPHA) -> dict[str, float]:
    """Compute EWMA per category from pulse history (oldest-first traversal).

    Returns the final EWMA value per category. On empty history, returns
    an empty dict (cold start — status will default to "low").
    """
    sorted_pulses = sorted(pulses, key=lambda p: p.get("generated_at", ""))

    ewma: dict[str, float] = {}
    for p in sorted_pulses:
        cats = p.get("categories") or {}
        for cat_name in CATEGORY_SOURCES:
            cat_data = cats.get(cat_name, {})
            count = cat_data.get("count")
            if count is None:
                continue
            if cat_name not in ewma:
                ewma[cat_name] = float(count)
            else:
                ewma[cat_name] = alpha * count + (1 - alpha) * ewma[cat_name]

    return {cat: round(val, 2) for cat, val in ewma.items()}


SIGMA_FACTOR = 0.15
SIGMA_MIN = 1.0
TREND_THRESHOLD = 0.05


def _sigma(ewma: float) -> float:
    return max(ewma * SIGMA_FACTOR, SIGMA_MIN)


def determine_status(alert_count: float, ewma: float | None) -> str:
    if alert_count == 0:
        return "clear"
    if ewma is None or ewma == 0:
        if alert_count > 0:
            return "moderate"
        return "low"
    sigma = _sigma(ewma)
    if alert_count > ewma + 2 * sigma:
        return "high"
    if alert_count > ewma + sigma:
        return "moderate"
    return "low"


def determine_trend(
    current_ewma: float | None, previous_ewma: float | None,
) -> str:
    if current_ewma is None or previous_ewma is None:
        return "stable"
    if previous_ewma == 0:
        return "worsening" if current_ewma > 0 else "stable"
    change = (current_ewma - previous_ewma) / previous_ewma
    if change > TREND_THRESHOLD:
        return "worsening"
    if change < -TREND_THRESHOLD:
        return "improving"
    return "stable"


def compute_categories(
    alerts: list[dict],
    previous_pulse: dict | None,
    history_pulses: list[dict],
    current_hour: int,
    now: datetime | None = None,
) -> dict:
    counts = count_alerts_by_category(alerts, now=now)
    current_ewma = compute_ewma(history_pulses)

    previous_ewma: dict[str, float | None] = {}
    if len(history_pulses) >= 2:
        previous_ewma = compute_ewma(history_pulses[:-1])

    categories = {}
    for cat_name in CATEGORY_SOURCES:
        count = counts[cat_name]
        cat_ewma = current_ewma.get(cat_name)
        prev_ewma = previous_ewma.get(cat_name)
        status = determine_status(count, cat_ewma)
        trend = determine_trend(cat_ewma, prev_ewma)
        categories[cat_name] = {
            "status": status,
            "trend": trend,
            "count": count,
            "ewma": cat_ewma if cat_ewma is not None else 0.0,
        }

    return categories


def compute_travel_ok(categories: dict) -> bool:
    for cat_name in ("transport", "roadworks"):
        cat = categories.get(cat_name, {})
        if cat.get("status") in ("moderate", "high"):
            return False
    return True
