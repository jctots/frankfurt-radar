#!/usr/bin/env python3
"""City Pulse — hourly AI-generated situational summary for Frankfurt."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import yaml

import db
from districts import coords_to_district
from pulse_categories import (
    build_category_timeseries,
    compute_snapshot,
)

log = logging.getLogger(__name__)

_GEMINI_URL_TPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

_health = {"ok": True}


def pulse_ok() -> bool:
    return _health["ok"]


def reset_pulse_health() -> None:
    _health["ok"] = True


def load_prompt(name: str) -> tuple[dict, str]:
    data_dir = Path(os.getenv("DATA_DIR", "."))
    prompt_path = data_dir / "prompts" / f"{name}.md"
    if not prompt_path.exists():
        prompt_path = Path(__file__).parent / "prompts" / f"{name}.md"
    text = prompt_path.read_text(encoding="utf-8")
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    config = yaml.safe_load(parts[1]) or {}
    template = parts[2].strip()
    return config, template


def _age_label(valid_from: str | None) -> str:
    if not valid_from:
        return "unknown"
    try:
        dt = datetime.fromisoformat(valid_from)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        hours = age.total_seconds() / 3600
        if hours < 1:
            return "NEW (< 1h)"
        if hours < 6:
            return f"recent ({int(hours)}h ago)"
        if hours < 24:
            return "today"
        days = int(hours / 24)
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days} days ago"
        return f"{days} days ago (low priority)"
    except (ValueError, TypeError):
        return "unknown"


def _to_berlin_iso(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).astimezone(ZoneInfo("Europe/Berlin")).isoformat()
    except ValueError:
        return iso


def _build_alert_data(alerts: list[dict]) -> tuple[str, str]:
    fresh = []
    stale_counts: dict[str, int] = {}
    for a in alerts:
        if a.get("stale"):
            src = a.get("source", "unknown")
            stale_counts[src] = stale_counts.get(src, 0) + 1
        else:
            source = a.get("source")
            body = "" if source == "baustellen" else (a.get("body_en") or "")[:500]
            entry = {
                "alert_id": a.get("alert_id"),
                "source": source,
                "title": a.get("title_en", ""),
                "body": body,
                "service": a.get("service"),
                "lines": json.loads(a["lines"]) if isinstance(a.get("lines"), str) else (a.get("lines") or []),
                "severity": a.get("severity"),
                "valid_from": _to_berlin_iso(a.get("valid_from")),
                "valid_until": _to_berlin_iso(a.get("valid_until")),
                "age": _age_label(a.get("valid_from")),
            }
            if a.get("location_label"):
                entry["location_label"] = a["location_label"]
            district = coords_to_district(a.get("lat"), a.get("lon"))
            if district:
                entry["district"] = district
            fresh.append(entry)

    fresh.sort(key=lambda x: x.get("valid_from") or "", reverse=True)

    alerts_json = json.dumps(fresh, ensure_ascii=False, indent=2)
    if stale_counts:
        stale_summary = ", ".join(f"{count} {src}" for src, count in stale_counts.items())
    else:
        stale_summary = "None"
    return alerts_json, stale_summary


def _build_history_section(pulses: list[dict], daily_summaries: list[dict] | None = None) -> str:
    parts = []
    if pulses:
        lines = ["HOURLY PULSES (last 3 hours — use for short-term trend changes):"]
        for p in pulses:
            cats = p.get("categories") or {}
            cat_parts = []
            for key in ("weather", "transport", "roadworks", "incidents", "events"):
                cat = cats.get(key)
                if cat:
                    cat_parts.append(f"{key}={cat.get('status','?')}/{cat.get('trend','?')}")
            cat_str = ", ".join(cat_parts) if cat_parts else "no categories"
            lines.append(
                f"- {p['generated_at']}: {p['summary']} "
                f"({cat_str})"
            )
        parts.append("\n".join(lines))
    if daily_summaries:
        lines = ["DAILY SUMMARIES (last 3 days — use for multi-day pattern detection):"]
        for ds in daily_summaries:
            lines.append(f"- {ds['date']}: {ds['summary']}")
        parts.append("\n".join(lines))
    if not parts:
        return "No history available — this is the first pulse."
    return "\n\n".join(parts)


def _call_gemini(prompt_config: dict, prompt_text: str, service: str = "gemini_pulse") -> tuple[dict, dict]:
    """Returns (result_dict, usage_dict). usage_dict has tokens_in/tokens_out/tokens_thinking."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — pulse skipped")
        return {}, {}

    model = prompt_config.get("model", "gemini-2.5-flash")
    url = _GEMINI_URL_TPL.format(model=model)

    gen_config = {
        "temperature": prompt_config.get("temperature", 0.3),
        "maxOutputTokens": prompt_config.get("max_output_tokens", 4096),
    }
    if prompt_config.get("response_mime_type"):
        gen_config["responseMimeType"] = prompt_config["response_mime_type"]
    if "thinking_budget" in prompt_config:
        gen_config["thinkingConfig"] = {"thinkingBudget": prompt_config["thinking_budget"]}

    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": gen_config,
    }

    for attempt in range(3):
        try:
            resp = requests.post(url, params={"key": api_key}, json=body, timeout=60)
            if resp.status_code == 429:
                wait = min(2 ** attempt * 5, 30)
                log.warning("Gemini rate limited (attempt %d/3), retrying in %ds", attempt + 1, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            raw_usage = data.get("usageMetadata", {})
            usage = {
                "tokens_in": raw_usage.get("promptTokenCount", 0),
                "tokens_out": raw_usage.get("candidatesTokenCount", 0),
                "tokens_thinking": raw_usage.get("thoughtsTokenCount", 0),
            }
            if raw_usage:
                db.record_api_usage(service, **usage)
            candidates = data.get("candidates", [])
            if not candidates:
                log.error("Gemini returned no candidates for pulse")
                _health["ok"] = False
                return {}, usage
            parts = candidates[0]["content"]["parts"]
            raw = parts[-1]["text"]
            return json.loads(raw), usage
        except requests.RequestException as e:
            log.error("Gemini pulse request failed: %s", e)
            _health["ok"] = False
            return {}, {}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            log.error("Gemini pulse response parse failed: %s", e)
            _health["ok"] = False
            return {}, {}

    log.error("Gemini rate limited — all retries exhausted for pulse")
    _health["ok"] = False
    return {}, {}


_PULSE_DEBUG_DIR = Path(os.getenv("DATA_DIR", ".")) / "pulse_debug"
_PULSE_DEBUG_RETENTION_DAYS = 30


def _write_debug_log(debug_data: dict) -> None:
    try:
        _PULSE_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _PULSE_DEBUG_DIR / f"{today}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(debug_data, ensure_ascii=False) + "\n")

        cutoff = (datetime.now(timezone.utc) - timedelta(days=_PULSE_DEBUG_RETENTION_DAYS)).strftime("%Y-%m-%d")
        for old in _PULSE_DEBUG_DIR.glob("*.jsonl"):
            if old.stem < cutoff:
                old.unlink(missing_ok=True)
        for old in _PULSE_DEBUG_DIR.glob("*.json"):
            old.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Pulse debug log write failed: %s", e)


_LEVEL_2_PLUS = {"moderate", "severe"}
# Only fast-changing categories trigger the 1h override — daily-interval categories
# (roadworks, events, incidents) don't change meaningfully within an hour.
_FAST_CATEGORIES = frozenset(("transport", "weather"))


def _should_skip_pulse(now: datetime) -> dict | None:
    """Return skip info dict if pulse should be skipped, None otherwise."""
    last = db.get_latest_pulse()
    if not last:
        return None

    last_cats = last.get("categories", {})
    elevated = any(
        cat.get("status") in _LEVEL_2_PLUS
        for key, cat in last_cats.items()
        if key in _FAST_CATEGORIES
    )
    if elevated:
        return None

    berlin = now.astimezone(ZoneInfo("Europe/Berlin"))
    night = berlin.hour >= 23 or berlin.hour < 6
    interval_hours = 3 if night else 2
    mode = "night" if night else "daytime"

    last_ts = datetime.fromisoformat(last["generated_at"].replace("Z", "+00:00"))
    now_slot = now.replace(minute=0, second=0, microsecond=0)
    last_slot = last_ts.replace(minute=0, second=0, microsecond=0)
    elapsed = (now_slot - last_slot).total_seconds() / 3600
    remaining_min = (interval_hours - elapsed) * 60
    if elapsed < interval_hours:
        log.info("Pulse skipped: all calm, next pulse in %.0f min", remaining_min)
        return {
            "reason": f"all calm ({mode}, {interval_hours}h interval), next in {remaining_min:.0f} min",
            "interval_hours": interval_hours,
            "elapsed_hours": round(elapsed, 2),
            "mode": mode,
            "last_categories": {k: v.get("status") for k, v in last_cats.items()},
        }
    return None


_ALL_CLEAR_PULSE = {
    "summary": "All clear — no active alerts in Frankfurt.",
    "categories": {
        "weather": {"status": "clear", "trend": "stable"},
        "transport": {"status": "clear", "trend": "stable"},
        "roadworks": {"status": "clear", "trend": "stable"},
        "incidents": {"status": "clear", "trend": "stable"},
        "events": {"status": "clear", "trend": "stable"},
    },
    "recommendation": "No special action needed.",
    "references": [],
}


def generate_pulse(config: dict, *, force: bool = False) -> dict | None:
    if not config.get("pulse", {}).get("enabled", False):
        log.debug("Pulse disabled in config")
        return None

    alerts = db.get_all_active_alerts()
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot, score_breakdown = compute_snapshot(alerts, now)
    snapshot_ts = now.strftime("%Y-%m-%dT%H:00:00Z")
    db.store_category_snapshots(snapshot_ts, snapshot)

    if not force:
        skip_info = _should_skip_pulse(now)
        if skip_info:
            _write_debug_log({
                "generated_at": generated_at,
                "service": "gemini_pulse",
                "skipped": True,
                **skip_info,
            })
            return None

    if not alerts:
        pulse = dict(_ALL_CLEAR_PULSE)
        pulse["generated_at"] = generated_at
        pulse["alert_count"] = 0
        db.store_pulse(pulse)
        log.info("Pulse generated: all clear (0 alerts)")
        return pulse

    timeseries = build_category_timeseries(db.get_category_snapshots, snapshot, now)

    prompt_config, template = load_prompt("pulse")
    alerts_json, stale_summary = _build_alert_data(alerts)
    history = _build_history_section(db.get_recent_pulses(3), db.get_recent_daily_summaries(3))
    _BERLIN = ZoneInfo("Europe/Berlin")
    now_berlin = now.astimezone(_BERLIN)
    timestamp = now_berlin.strftime("%Y-%m-%d %H:%M %Z")

    fresh_count = sum(1 for a in alerts if not a.get("stale"))
    prompt_text = template.format_map({
        "timestamp": timestamp,
        "alert_count": fresh_count,
        "alerts_json": alerts_json,
        "stale_summary": stale_summary,
        "history_section": history,
        "timeseries_json": json.dumps(timeseries, indent=2),
    })

    result, usage = _call_gemini(prompt_config, prompt_text)
    if not result:
        return None

    references = result.get("references") or []
    valid_ids = {a.get("alert_id") for a in alerts if a.get("alert_id")}
    references = [r for r in references if r in valid_ids][:3]

    # Status is deterministic from Layer 1; LLM provides trend only
    _VALID_TRENDS = {"stable", "improving", "worsening"}
    llm_categories = result.get("categories", {})
    categories = {}
    for cat in ("weather", "transport", "roadworks", "incidents", "events"):
        computed_status = timeseries.get(cat, {}).get("current", {}).get("status", "clear")
        llm_trend = (llm_categories.get(cat) or {}).get("trend", "stable")
        if llm_trend not in _VALID_TRENDS:
            llm_trend = "stable"
        categories[cat] = {"status": computed_status, "trend": llm_trend}

    pulse = {
        "generated_at": generated_at,
        "title": result.get("title", ""),
        "summary": result.get("summary", ""),
        "categories": categories,
        "recommendation": result.get("recommendation", ""),
        "alert_count": len(alerts),
        "references": references,
    }
    db.store_pulse(pulse)
    log.info("Pulse generated: %d alerts", len(alerts))

    _write_debug_log({
        "generated_at": generated_at,
        "current_hour_utc": now.hour,
        "service": "gemini_pulse",
        "usage": usage,
        "layer_1_deterministic": {
            "timeseries": timeseries,
            "score_breakdown": score_breakdown,
            "total_alerts": len(alerts),
            "fresh_alerts": fresh_count,
            "stale_summary": stale_summary,
        },
        "layer_2_llm": {
            "model": prompt_config.get("model", "gemini-2.5-flash"),
            "prompt": prompt_text,
            "response": result,
        },
        "layer_3_output": pulse,
    })

    return pulse


def generate_daily_summary(config: dict, date: str | None = None) -> dict | None:
    if not config.get("pulse", {}).get("enabled", False):
        log.debug("Pulse disabled in config — daily summary skipped")
        return None

    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Berlin")
    if date is None:
        date = datetime.now(tz).strftime("%Y-%m-%d")

    pulses = db.get_pulses_for_date(date)
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not pulses:
        summary = f"No pulse data available for {date}."
        db.store_daily_summary(date, summary, generated_at)
        log.info("Daily summary for %s: no pulses", date)
        return {"date": date, "summary": summary, "generated_at": generated_at}

    prompt_config, template = load_prompt("daily_summary")
    previous = db.get_recent_daily_summaries(3)
    prev_text = "\n".join(f"- {ds['date']}: {ds['summary']}" for ds in previous) if previous else "None"

    pulses_for_prompt = []
    for p in pulses:
        pulses_for_prompt.append({
            "time": p["generated_at"],
            "summary": p["summary"],
            "categories": p["categories"],
        })

    prompt_text = template.format_map({
        "date": date,
        "pulse_count": len(pulses),
        "pulses_json": json.dumps(pulses_for_prompt, ensure_ascii=False, indent=2),
        "previous_summaries": prev_text,
    })

    result, usage = _call_gemini(prompt_config, prompt_text, service="gemini_daily")
    if not result:
        return None

    summary = result.get("summary", "")
    db.store_daily_summary(date, summary, generated_at)
    log.info("Daily summary for %s: %d pulses summarized", date, len(pulses))

    _write_debug_log({
        "generated_at": generated_at,
        "service": "gemini_daily",
        "usage": usage,
        "date_summarized": date,
        "pulse_count": len(pulses),
    })

    return {"date": date, "summary": summary, "generated_at": generated_at, **result}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "."))
    cfg = yaml.safe_load((data_dir / "config.yaml").read_text()) or {}
    db.init_db()

    args = sys.argv[1:]
    if "--daily" in args:
        date = args[args.index("--daily") + 1] if len(args) > args.index("--daily") + 1 else None
        result = generate_daily_summary(cfg, date)
    else:
        result = generate_pulse(cfg, force="--force" in args)

    if result:
        print(json.dumps(result, indent=2))
    else:
        print("Pulse generation skipped or failed")
