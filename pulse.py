#!/usr/bin/env python3
"""City Pulse — hourly AI-generated situational summary for Frankfurt."""
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import db
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


def _build_alert_data(alerts: list[dict]) -> tuple[str, str]:
    fresh = []
    stale_counts: dict[str, int] = {}
    for a in alerts:
        if a.get("stale"):
            src = a.get("source", "unknown")
            stale_counts[src] = stale_counts.get(src, 0) + 1
        else:
            fresh.append({
                "alert_id": a.get("alert_id"),
                "source": a.get("source"),
                "title": a.get("title_en", ""),
                "body": (a.get("body_en") or "")[:500],
                "service": a.get("service"),
                "lines": json.loads(a["lines"]) if isinstance(a.get("lines"), str) else (a.get("lines") or []),
                "severity": a.get("severity"),
                "valid_from": a.get("valid_from"),
                "valid_until": a.get("valid_until"),
                "age": _age_label(a.get("valid_from")),
            })

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


def _call_gemini(prompt_config: dict, prompt_text: str, service: str = "gemini_pulse") -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — pulse skipped")
        return {}

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
            usage = data.get("usageMetadata", {})
            if usage:
                db.record_api_usage(
                    service,
                    tokens_in=usage.get("promptTokenCount", 0),
                    tokens_out=usage.get("candidatesTokenCount", 0)
                        + usage.get("thoughtsTokenCount", 0),
                )
            candidates = data.get("candidates", [])
            if not candidates:
                log.error("Gemini returned no candidates for pulse")
                _health["ok"] = False
                return {}
            parts = candidates[0]["content"]["parts"]
            raw = parts[-1]["text"]
            return json.loads(raw)
        except requests.RequestException as e:
            log.error("Gemini pulse request failed: %s", e)
            _health["ok"] = False
            return {}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            log.error("Gemini pulse response parse failed: %s", e)
            _health["ok"] = False
            return {}

    log.error("Gemini rate limited — all retries exhausted for pulse")
    _health["ok"] = False
    return {}


_PULSE_DEBUG_DIR = Path(os.getenv("DATA_DIR", ".")) / "pulse_debug"
_PULSE_DEBUG_RETENTION_DAYS = 30


def _write_debug_log(debug_data: dict) -> None:
    try:
        _PULSE_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        ts = debug_data.get("generated_at", "")[:13].replace(":", "")
        path = _PULSE_DEBUG_DIR / f"{ts}.json"
        path.write_text(json.dumps(debug_data, ensure_ascii=False, indent=2))

        cutoff = datetime.now(timezone.utc) - timedelta(days=_PULSE_DEBUG_RETENTION_DAYS)
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H")
        for old in _PULSE_DEBUG_DIR.glob("*.json"):
            if old.stem < cutoff_str:
                old.unlink(missing_ok=True)
    except OSError as e:
        log.warning("Pulse debug log write failed: %s", e)


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


def generate_pulse(config: dict) -> dict | None:
    if not config.get("pulse", {}).get("enabled", False):
        log.debug("Pulse disabled in config")
        return None

    alerts = db.get_all_active_alerts()
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    snapshot, score_breakdown = compute_snapshot(alerts, now)
    snapshot_ts = now.strftime("%Y-%m-%dT%H:00:00Z")
    db.store_category_snapshots(snapshot_ts, snapshot)

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
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    fresh_count = sum(1 for a in alerts if not a.get("stale"))
    prompt_text = template.format_map({
        "timestamp": timestamp,
        "alert_count": fresh_count,
        "alerts_json": alerts_json,
        "stale_summary": stale_summary,
        "history_section": history,
        "timeseries_json": json.dumps(timeseries, indent=2),
    })

    result = _call_gemini(prompt_config, prompt_text)
    if not result:
        return None

    references = result.get("references") or []
    valid_ids = {a.get("alert_id") for a in alerts if a.get("alert_id")}
    references = [r for r in references if r in valid_ids][:3]

    categories = result.get("categories", {})
    for cat in ("weather", "transport", "roadworks", "incidents", "events"):
        if cat not in categories:
            categories[cat] = {"status": "clear", "trend": "stable"}

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

    result = _call_gemini(prompt_config, prompt_text, service="gemini_daily")
    if not result:
        return None

    summary = result.get("summary", "")
    db.store_daily_summary(date, summary, generated_at)
    log.info("Daily summary for %s: %d pulses summarized", date, len(pulses))
    return {"date": date, "summary": summary, "generated_at": generated_at, **result}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data_dir = Path(os.getenv("DATA_DIR", "."))
    cfg = yaml.safe_load((data_dir / "config.yaml").read_text()) or {}
    db.init_db()

    if len(sys.argv) > 1 and sys.argv[1] == "--daily":
        date = sys.argv[2] if len(sys.argv) > 2 else None
        result = generate_daily_summary(cfg, date)
    else:
        result = generate_pulse(cfg)

    if result:
        print(json.dumps(result, indent=2))
    else:
        print("Pulse generation skipped or failed")
