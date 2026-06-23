"""Deterministic category status and trend computation for City Pulse.

Categories are computed from active alert counts, not by the LLM. This gives
consistent, explainable results and handles cold-start gracefully.

Status levels (clear → low → moderate → high) are determined by comparing
the current alert count against an EWMA (Exponential Weighted Moving Average)
baseline computed from pulse history. α=0.3 balances responsiveness with
stability — long-running alerts naturally fade into the baseline.

Trends compare the current count against the EWMA: >1.3× = worsening,
<0.7× = improving, else stable. This avoids the cross-hour comparison
problem of the previous status-level-based approach.

Source-to-category mapping:
  weather    = dwd
  transport  = rmv
  roadworks  = autobahn, baustellen
  incidents  = polizei, strike
  events     = events, sports
"""

from __future__ import annotations

CATEGORY_SOURCES: dict[str, list[str]] = {
    "weather": ["dwd"],
    "transport": ["rmv"],
    "roadworks": ["autobahn", "baustellen"],
    "incidents": ["polizei", "strike"],
    "events": ["events", "sports"],
}

STATUS_LEVELS = ("clear", "low", "moderate", "high")

EWMA_ALPHA = 0.3


def count_alerts_by_category(alerts: list[dict]) -> dict[str, int]:
    source_to_cat = {}
    for cat, sources in CATEGORY_SOURCES.items():
        for src in sources:
            source_to_cat[src] = cat

    counts: dict[str, int] = {cat: 0 for cat in CATEGORY_SOURCES}
    for alert in alerts:
        if alert.get("stale"):
            continue
        cat = source_to_cat.get(alert.get("source", ""))
        if cat:
            counts[cat] += 1
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


def determine_status(alert_count: int, ewma: float | None) -> str:
    if alert_count == 0:
        return "clear"
    if ewma is None or ewma == 0:
        if alert_count > 0:
            return "moderate"
        return "low"
    ratio = alert_count / ewma
    if ratio <= 1.3:
        return "low"
    if ratio <= 1.6:
        return "moderate"
    return "high"


def determine_trend(alert_count: int, ewma: float | None) -> str:
    if ewma is None or ewma == 0:
        return "stable"
    ratio = alert_count / ewma
    if ratio > 1.3:
        return "worsening"
    if ratio < 0.7:
        return "improving"
    return "stable"


def compute_categories(
    alerts: list[dict],
    previous_pulse: dict | None,
    history_pulses: list[dict],
    current_hour: int,
) -> dict:
    counts = count_alerts_by_category(alerts)
    ewma = compute_ewma(history_pulses)

    categories = {}
    for cat_name in CATEGORY_SOURCES:
        count = counts[cat_name]
        cat_ewma = ewma.get(cat_name)
        status = determine_status(count, cat_ewma)
        trend = determine_trend(count, cat_ewma)
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
