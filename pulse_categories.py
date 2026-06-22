"""Deterministic category status and trend computation for City Pulse.

Categories are computed from active alert counts, not by the LLM. This gives
consistent, explainable results and handles cold-start gracefully.

Status levels (clear → low → moderate → high) are determined by comparing
the current alert count against a 7-day rolling average at the same hour of
day (±1 hour window). The baseline self-corrects: if roadworks are always
high, that becomes the new normal and the status stays "low".

Trends compare the current status level against the previous pulse's level:
same = stable, higher = worsening, lower = improving. On cold start (no
previous pulse), all trends default to "stable".

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


def get_baseline(pulses: list[dict], current_hour: int) -> dict[str, float]:
    hour_window = {(current_hour - 1) % 24, current_hour, (current_hour + 1) % 24}

    totals: dict[str, list[int]] = {cat: [] for cat in CATEGORY_SOURCES}
    for p in pulses:
        generated_at = p.get("generated_at", "")
        if len(generated_at) < 13:
            continue
        try:
            pulse_hour = int(generated_at[11:13])
        except (ValueError, IndexError):
            continue
        if pulse_hour not in hour_window:
            continue

        cats = p.get("categories") or {}
        for cat_name in CATEGORY_SOURCES:
            cat_data = cats.get(cat_name, {})
            count = cat_data.get("count")
            if count is not None:
                totals[cat_name].append(count)

    return {
        cat: sum(counts) / len(counts)
        for cat, counts in totals.items()
        if counts
    }


def determine_status(alert_count: int, baseline_avg: float | None) -> str:
    if alert_count == 0:
        return "clear"
    if baseline_avg is None:
        return "low"
    if baseline_avg == 0:
        return "moderate"
    ratio = alert_count / baseline_avg
    if ratio <= 1.1:
        return "low"
    if ratio <= 1.6:
        return "moderate"
    return "high"


def determine_trend(current_status: str, previous_status: str | None) -> str:
    if previous_status is None:
        return "stable"
    try:
        current_idx = STATUS_LEVELS.index(current_status)
        previous_idx = STATUS_LEVELS.index(previous_status)
    except ValueError:
        return "stable"
    if current_idx > previous_idx:
        return "worsening"
    if current_idx < previous_idx:
        return "improving"
    return "stable"


def compute_categories(
    alerts: list[dict],
    previous_pulse: dict | None,
    history_pulses: list[dict],
    current_hour: int,
) -> dict:
    counts = count_alerts_by_category(alerts)
    baseline = get_baseline(history_pulses, current_hour)

    prev_cats = (previous_pulse or {}).get("categories") or {}

    categories = {}
    for cat_name in CATEGORY_SOURCES:
        count = counts[cat_name]
        avg = baseline.get(cat_name)
        status = determine_status(count, avg)

        prev_status = None
        prev_cat = prev_cats.get(cat_name)
        if prev_cat:
            prev_status = prev_cat.get("status")

        trend = determine_trend(status, prev_status)
        categories[cat_name] = {"status": status, "trend": trend, "count": count}

    return categories
