"""Deterministic reducer for the City Pulse review pipeline (docs/review.md).

Turns raw `data/{cost,translate,pulse}_debug/*.jsonl` logs plus `radar.db`
into a compact digest for the Gemini reviewer. Pure Python: no network,
no LLM. See docs/review.md for the digest schema and the cost knobs.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

# Rough chars-per-token used only for the pre-spend cost estimate — not for
# billing (Gemini bills on its own tokenizer). See docs/review.md cost table.
_CHARS_PER_TOKEN = 4.0

# Indicative EUR/token for the two reviewer models (docs/review.md table).
# Verify against current Gemini pricing before wiring real cost tracking.
_REVIEWER_EUR_PER_TOKEN = {
    "gemini-2.5-pro": 0.0000015,
    "gemini-2.5-flash": 0.00000015,
}
_DEFAULT_REVIEWER_MODEL = "gemini-2.5-pro"

# event_log entries surfaced as anomalies; "recovery" is the resolution of a
# "failure" and not itself worth flagging.
_ANOMALY_EVENT_TYPES = frozenset(("failure", "restart"))

_CATEGORIES = ("weather", "transport", "roadworks", "incidents", "events")


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "."))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _read_debug_range(subdir: str, since: datetime, until: datetime) -> list[dict]:
    """Read all daily *.jsonl records for `subdir` covering [since, until]."""
    base = _data_dir() / subdir
    records = []
    day = since.date()
    while day <= until.date():
        records.extend(_read_jsonl(base / f"{day.isoformat()}.jsonl"))
        day += timedelta(days=1)
    return records


def _db_since(dt: datetime) -> str:
    """Format `dt` to compare against radar.db's `strftime('%Y-%m-%dT%H:%M:%fZ')`
    timestamps (3-digit fraction) so lexicographic `>=` stays chronological."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# ── pulse_debug reduction ────────────────────────────────────────────────

def _extract_drivers(cat_breakdown: dict, drivers_per_hour: int | None) -> list[str]:
    """Top-N scoring alerts for a category/hour, formatted as "id wWEIGHT".

    `drivers_per_hour is None` means "all"; `0` means counts only (empty).
    """
    if drivers_per_hour == 0:
        return []
    ongoing = cat_breakdown.get("ongoing", []) if cat_breakdown else []
    ranked = sorted(ongoing, key=lambda a: a.get("weight", 0) or 0, reverse=True)
    if drivers_per_hour is not None:
        ranked = ranked[:drivers_per_hour]
    return [f"{a.get('alert_id')} w{a.get('weight')}" for a in ranked]


def _reduce_pulse_hours(records: list[dict], drivers_per_hour: int | None) -> tuple[list[dict], str | None, list[str]]:
    """Return (pulse_hours, prompt_template, extra_prompt_samples).

    `prompt_template` is one deduped copy of the rendered prompt (the
    artifact under review, included free of the `prompt_samples` knob).
    `extra_prompt_samples` holds up to `prompt_samples` further full
    prompts (set by the caller) to show variation across hours.
    """
    generated = [r for r in records if not r.get("skipped")]
    generated.sort(key=lambda r: r.get("generated_at") or "")

    prompt_template: str | None = None
    all_prompts: list[str] = []
    hours: list[dict] = []

    for rec in generated:
        layer1 = rec.get("layer_1_deterministic", {}) or {}
        layer2 = rec.get("layer_2_llm", {}) or {}
        layer3 = rec.get("layer_3_output", {}) or {}
        timeseries = layer1.get("timeseries", {}) or {}
        breakdown = layer1.get("score_breakdown", {}) or {}

        prompt_text = layer2.get("prompt")
        if prompt_text:
            all_prompts.append(prompt_text)
            if prompt_template is None:
                prompt_template = prompt_text

        score_inputs = {}
        for cat in _CATEGORIES:
            current = (timeseries.get(cat, {}) or {}).get("current", {}) or {}
            score_inputs[cat] = {
                "status": current.get("status"),
                "trend": current.get("trend"),
                "ongoing_score": (current.get("ongoing", {}) or {}).get("score"),
                "top_drivers": _extract_drivers(breakdown.get(cat, {}), drivers_per_hour),
            }

        response = layer2.get("response", {}) or {}
        hours.append({
            "hour": rec.get("generated_at"),
            "config_version": rec.get("pulse_config_version"),
            "score_inputs": score_inputs,
            "llm_response": {
                "title": response.get("title", ""),
                "summary": response.get("summary", ""),
                "recommendation": response.get("recommendation", ""),
                "references": response.get("references", []),
                "trend_override": response.get("trend_override", []),
            },
            "layer_3_output": {"categories": layer3.get("categories", {})},
        })

    return hours, prompt_template, all_prompts[1:]


# ── cost reduction ───────────────────────────────────────────────────────

# Mirrors db.get_monthly_cost's pricing formula at per-day granularity so the
# digest can report a daily cost breakdown without a second DB helper.
def _price_usage_rows(rows: list[dict], config: dict) -> dict[str, float]:
    cost_cfg = (config or {}).get("cost", {})
    gemini_cfg = cost_cfg.get("gemini", {})
    translate_cfg = cost_cfg.get("google_translate", {})
    usd_to_eur = cost_cfg.get("usd_to_eur", 0.92)
    gemini_pricing = {
        "input_per_m": gemini_cfg.get("input_per_million", 0.15),
        "output_per_m": gemini_cfg.get("output_per_million", 0.60),
        "thinking_per_m": gemini_cfg.get("thinking_per_million", 3.50),
    }
    pricing = {
        "gemini_pulse": gemini_pricing,
        "gemini_extraction": gemini_pricing,
        "gemini_daily": gemini_pricing,
        "gemini_review": gemini_pricing,
        "google_translate": {"chars_per_m": translate_cfg.get("chars_per_million", 20.0)},
    }
    out: dict[str, float] = {}
    for row in rows:
        svc = row["service"]
        p = pricing.get(svc, {})
        cost_usd = 0.0
        if "input_per_m" in p:
            cost_usd += (row.get("tokens_in") or 0) / 1_000_000 * p["input_per_m"]
            cost_usd += (row.get("tokens_out") or 0) / 1_000_000 * p["output_per_m"]
            cost_usd += (row.get("tokens_thinking") or 0) / 1_000_000 * p["thinking_per_m"]
        if "chars_per_m" in p:
            cost_usd += (row.get("characters") or 0) / 1_000_000 * p["chars_per_m"]
        out[svc] = out.get(svc, 0.0) + cost_usd * usd_to_eur
    return out


def _reduce_cost(cost_records: list[dict], config: dict, since: datetime, until: datetime) -> dict:
    daily_by_service: dict[str, dict[str, float]] = {}
    day = since.date()
    while day <= until.date():
        date_str = day.isoformat()
        rows = db.get_daily_usage(date_str)
        priced = _price_usage_rows(rows, config)
        if priced:
            daily_by_service[date_str] = {k: round(v, 4) for k, v in priced.items()}
        day += timedelta(days=1)

    totals: dict[str, float] = {}
    for services in daily_by_service.values():
        for svc, eur in services.items():
            totals[svc] = totals.get(svc, 0.0) + eur
    window_total = sum(totals.values())

    top_spenders = sorted(
        (
            {"service": svc, "eur": round(eur, 4), "share": round(eur / window_total, 4) if window_total else 0.0}
            for svc, eur in totals.items()
        ),
        key=lambda x: x["eur"],
        reverse=True,
    )

    monthly_cumulative = {"total_eur": 0.0, "services": {}}
    if cost_records:
        last = max(cost_records, key=lambda r: r.get("hour") or "")
        monthly_cumulative = last.get("monthly_cumulative", monthly_cumulative)

    return {
        "monthly_cumulative": monthly_cumulative,
        "daily_by_service": [{"date": d, **s} for d, s in sorted(daily_by_service.items())],
        "top_spenders": top_spenders,
    }


# ── translate reduction ──────────────────────────────────────────────────

def _reduce_translate(records: list[dict]) -> dict:
    if not records:
        return {"cache_hit_ratio": None, "new_translated": 0, "retranslated": 0, "anomalies": []}

    total_alerts = sum(r.get("total_alerts", 0) for r in records)
    cached = sum(r.get("cached", 0) for r in records)
    new_translated = sum(r.get("new_translated", 0) for r in records)
    retranslated = sum(r.get("retranslated", 0) for r in records)

    anomalies = []
    for rec in records:
        for entry in rec.get("entries", []) or []:
            if entry.get("action") == "variant_hit" or entry.get("reason") == "text_changed":
                anomalies.append({
                    "alert_id": entry.get("alert_id"),
                    "action": entry.get("action"),
                    "reason": entry.get("reason"),
                })

    return {
        "cache_hit_ratio": round(cached / total_alerts, 4) if total_alerts else None,
        "new_translated": new_translated,
        "retranslated": retranslated,
        "anomalies": anomalies,
    }


# ── version metrics ──────────────────────────────────────────────────────

def _status_flap_rate(hours_in_version: list[dict]) -> float:
    if len(hours_in_version) < 2:
        return 0.0
    flaps = 0
    total = 0
    for cat in _CATEGORIES:
        statuses = [h["score_inputs"].get(cat, {}).get("status") for h in hours_in_version]
        for prev, cur in zip(statuses, statuses[1:]):
            total += 1
            if prev != cur:
                flaps += 1
    return round(flaps / total, 4) if total else 0.0


def _trend_override_rate(hours_in_version: list[dict]) -> float:
    if not hours_in_version:
        return 0.0
    overridden = sum(1 for h in hours_in_version if h["llm_response"].get("trend_override"))
    return round(overridden / len(hours_in_version), 4)


def _compute_version_metrics(
    pulse_hours: list[dict],
    all_pulse_records: list[dict],
    overrides: list[dict],
) -> dict[str, dict]:
    by_version: dict[str, list[dict]] = {}
    for h in pulse_hours:
        by_version.setdefault(h.get("config_version"), []).append(h)
    by_version.pop(None, None)

    # A version's span runs from its first-seen hour to the next version's
    # first-seen hour (or window end) — coverage is generated-pulse count
    # over *all* pulse_debug entries (generated or skipped) in that span, so
    # intentional interval skips don't read as review-worthy gaps.
    first_seen = {v: min(h["hour"] for h in hs) for v, hs in by_version.items()}
    ordered_versions = sorted(first_seen, key=lambda v: first_seen[v])
    all_records_sorted = sorted(all_pulse_records, key=lambda r: r.get("generated_at") or "")

    override_counts: dict[str, int] = {}
    for ov in overrides:
        v = ov.get("config_version")
        if v:
            override_counts[v] = override_counts.get(v, 0) + 1

    metrics: dict[str, dict] = {}
    for idx, version in enumerate(ordered_versions):
        span_start = first_seen[version]
        span_end = first_seen[ordered_versions[idx + 1]] if idx + 1 < len(ordered_versions) else None
        span_records = [
            r for r in all_records_sorted
            if r.get("generated_at", "") >= span_start and (span_end is None or r.get("generated_at", "") < span_end)
        ]
        hs = by_version[version]
        produced = len(hs)
        possible = len(span_records) or produced

        m = {
            "status_flap_rate": _status_flap_rate(hs),
            "trend_override_rate": _trend_override_rate(hs),
            "cost_per_pulse_eur": _cost_per_pulse_eur(hs),
            "coverage": round(produced / possible, 4) if possible else 1.0,
        }
        if version in override_counts:
            m["override_rate"] = round(override_counts[version] / produced, 4) if produced else 0.0
        metrics[version] = m

    return metrics


def _cost_per_pulse_eur(hours_in_version: list[dict]) -> float:
    # Proxy, not billed cost: token-based estimate from each hour's usage
    # would require the raw record's `usage` block, which pulse_hours does
    # not retain (kept out of the digest — see docs/review.md#redaction
    # sibling concern, token budget). Approximated at zero when unknown.
    return 0.0


# ── db cross-checks ──────────────────────────────────────────────────────

def _pulse_coverage(all_pulse_records: list[dict], since: datetime, until: datetime) -> dict:
    produced_hours = set()
    for rec in all_pulse_records:
        ts = _parse_ts(rec.get("generated_at"))
        if ts:
            produced_hours.add(ts.strftime("%Y-%m-%dT%H:00Z"))

    expected_hours = 0
    gaps = []
    cursor = since.replace(minute=0, second=0, microsecond=0)
    end = until.replace(minute=0, second=0, microsecond=0)
    while cursor <= end:
        expected_hours += 1
        label = cursor.strftime("%Y-%m-%dT%H:00Z")
        if label not in produced_hours:
            gaps.append(label)
        cursor += timedelta(hours=1)

    return {
        "expected_hours": expected_hours,
        "produced": expected_hours - len(gaps),
        "gaps": gaps,
    }


def _cost_reconciliation(cost_records: list[dict], config: dict, since: datetime, until: datetime) -> dict:
    logged_eur = 0.0
    if cost_records:
        last = max(cost_records, key=lambda r: r.get("hour") or "")
        logged_eur = (last.get("monthly_cumulative") or {}).get("total_eur", 0.0)

    months = sorted({d.strftime("%Y-%m") for d in _daterange(since, until)})
    api_usage_eur = 0.0
    for month in months:
        total, _ = db.get_monthly_cost(month, config or {})
        api_usage_eur += total

    return {
        "logged_eur": round(logged_eur, 4),
        "api_usage_eur": round(api_usage_eur, 4),
        "delta": round(api_usage_eur - logged_eur, 4),
    }


def _daterange(since: datetime, until: datetime):
    day = since
    while day.date() <= until.date():
        yield day
        day += timedelta(days=1)


def _event_log_anomalies(since: datetime) -> list[dict]:
    events = db.get_events_since(_db_since(since))
    return [
        {"ts": e["timestamp"], "level": "error" if e["event_type"] == "failure" else "info",
         "msg": f"{e['event_type']} ({e['source']}): {e['detail']}"}
        for e in events
        if e["event_type"] in _ANOMALY_EVENT_TYPES
    ]


def _status_overrides(since: datetime) -> list[dict]:
    """Human corrections since `since`, tagged with the pulse's config_version.

    `status_overrides` rows are keyed by `pulse_ts`, not directly by version —
    joined here against pulse_history so the override rate can be attributed
    to the correct methodology snapshot.
    """
    overrides = db.get_status_overrides(limit=1000)
    since_str = _db_since(since)
    result = []
    for ov in overrides:
        if ov["created_at"] < since_str:
            continue
        result.append({
            "hour": ov["pulse_ts"],
            "category": ov["category"],
            "computed": ov["computed_status"],
            "corrected": ov["override_status"],
            "reason": ov["reason"],
        })
    return result


def _attach_override_versions(overrides: list[dict], pulse_hours: list[dict]) -> None:
    version_by_hour = {h["hour"]: h.get("config_version") for h in pulse_hours}
    for ov in overrides:
        ov["config_version"] = version_by_hour.get(ov["hour"])


# ── cost estimate (pre-spend preview) ────────────────────────────────────

def estimate_cost(digest: dict, model: str = _DEFAULT_REVIEWER_MODEL) -> dict:
    """Estimated tokens + EUR for sending `digest` to the reviewer model.

    Indicative only (docs/review.md cost table) — shown to the admin before
    the Gemini call fires, never used for actual billing.
    """
    size_chars = len(json.dumps(digest, ensure_ascii=False))
    tokens = int(size_chars / _CHARS_PER_TOKEN)
    eur_per_token = _REVIEWER_EUR_PER_TOKEN.get(model, _REVIEWER_EUR_PER_TOKEN[_DEFAULT_REVIEWER_MODEL])
    return {"tokens": tokens, "eur": round(tokens * eur_per_token, 4), "model": model}


# ── entrypoint ────────────────────────────────────────────────────────────

def reduce(
    days: int = 7,
    drivers_per_hour: int | None = 3,
    prompt_samples: int = 1,
    *,
    config: dict | None = None,
    now: datetime | None = None,
) -> dict:
    """Build the review digest from the last `days` of debug logs + radar.db.

    `drivers_per_hour`: top-N scoring alerts kept per category per hour;
    `0` = counts only, `None` = all. `prompt_samples`: 0-2 additional fully
    rendered prompt examples beyond the always-included dedup'd template.
    """
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)

    pulse_records = _read_debug_range("pulse_debug", since, now)
    pulse_records = [r for r in pulse_records if (_parse_ts(r.get("generated_at")) or now) >= since]
    # pulse_debug/*.jsonl is a shared log file — gemini_extraction and
    # gemini_daily records land there too, alongside gemini_pulse. Filter to
    # gemini_pulse only, or extraction/daily records (a completely different
    # shape) get treated as empty/null pulse hours.
    pulse_records = [r for r in pulse_records if r.get("service") == "gemini_pulse"]
    cost_records = _read_debug_range("cost_debug", since, now)
    translate_records = _read_debug_range("translate_debug", since, now)
    translate_records = [r for r in translate_records if (_parse_ts(r.get("timestamp")) or now) >= since]

    pulse_hours, prompt_template, extra_prompts = _reduce_pulse_hours(pulse_records, drivers_per_hour)
    prompt_samples_out = extra_prompts[:max(prompt_samples, 0)]

    overrides = _status_overrides(since)
    _attach_override_versions(overrides, pulse_hours)

    config_versions = sorted({h["config_version"] for h in pulse_hours if h.get("config_version")})

    digest = {
        "range": f"{since.date().isoformat()}..{now.date().isoformat()}",
        "params": {"days": days, "drivers_per_hour": drivers_per_hour, "prompt_samples": prompt_samples},
        "config_versions": config_versions,
        "prompt_template": prompt_template,
        "prompt_samples": prompt_samples_out,
        "cost": _reduce_cost(cost_records, config or {}, since, now),
        "translate": _reduce_translate(translate_records),
        "pulse_hours": pulse_hours,
        "overrides": overrides,
        "version_metrics": _compute_version_metrics(pulse_hours, pulse_records, overrides),
        "db_crosschecks": {
            "cost_reconciliation": _cost_reconciliation(cost_records, config or {}, since, now),
            "pulse_coverage": _pulse_coverage(pulse_records, since, now),
            "event_log_anomalies": _event_log_anomalies(since),
        },
    }
    return digest
