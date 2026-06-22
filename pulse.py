#!/usr/bin/env python3
"""City Pulse — hourly AI-generated situational summary for Frankfurt."""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

import db

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


def _build_alert_data(alerts: list[dict]) -> tuple[str, str]:
    fresh = []
    stale_counts: dict[str, int] = {}
    for a in alerts:
        if a.get("stale"):
            src = a.get("source", "unknown")
            stale_counts[src] = stale_counts.get(src, 0) + 1
        else:
            fresh.append({
                "source": a.get("source"),
                "title": a.get("title_en", ""),
                "body": (a.get("body_en") or "")[:200],
                "service": a.get("service"),
                "lines": json.loads(a["lines"]) if isinstance(a.get("lines"), str) else (a.get("lines") or []),
                "severity": a.get("severity"),
                "valid_from": a.get("valid_from"),
                "valid_until": a.get("valid_until"),
            })

    alerts_json = json.dumps(fresh, ensure_ascii=False, indent=2)
    if stale_counts:
        stale_summary = ", ".join(f"{count} {src}" for src, count in stale_counts.items())
    else:
        stale_summary = "None"
    return alerts_json, stale_summary


def _build_history_section(pulses: list[dict], daily_summaries: list[dict] | None = None) -> str:
    parts = []
    if pulses:
        lines = ["Previous pulses (most recent first):"]
        for p in pulses:
            lines.append(f"- {p['generated_at']}: {p['summary']} (travel_ok={p['travel_ok']})")
        parts.append("\n".join(lines))
    if daily_summaries:
        lines = ["Previous daily summaries (most recent first):"]
        for ds in daily_summaries:
            lines.append(f"- {ds['date']}: {ds['summary']}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _call_gemini(prompt_config: dict, prompt_text: str) -> dict:
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
            resp = requests.post(url, params={"key": api_key}, json=body, timeout=30)
            if resp.status_code == 429:
                wait = min(2 ** attempt * 5, 30)
                log.warning("Gemini rate limited (attempt %d/3), retrying in %ds", attempt + 1, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            candidates = resp.json().get("candidates", [])
            if not candidates:
                log.error("Gemini returned no candidates for pulse")
                _health["ok"] = False
                return {}
            raw = candidates[0]["content"]["parts"][0]["text"]
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


_ALL_CLEAR_PULSE = {
    "summary": "All clear — no active alerts in Frankfurt.",
    "travel_ok": True,
    "categories": {
        "weather": {"status": "clear", "trend": "stable"},
        "transit": {"status": "normal", "trend": "stable"},
        "roads": {"status": "normal", "trend": "stable"},
        "highways": {"status": "normal", "trend": "stable"},
        "safety": {"status": "normal", "trend": "stable"},
        "events": {"status": "none", "trend": "stable"},
    },
    "recommendation": "No special action needed.",
}


def generate_pulse(config: dict) -> dict | None:
    if not config.get("pulse", {}).get("enabled", False):
        log.debug("Pulse disabled in config")
        return None

    alerts = db.get_all_active_alerts()
    now = datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if not alerts:
        pulse = dict(_ALL_CLEAR_PULSE)
        pulse["generated_at"] = generated_at
        pulse["alert_count"] = 0
        db.store_pulse(pulse)
        log.info("Pulse generated: all clear (0 alerts)")
        return pulse

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
    })

    result = _call_gemini(prompt_config, prompt_text)
    if not result:
        return None

    pulse = {
        "generated_at": generated_at,
        "summary": result.get("summary", ""),
        "travel_ok": result.get("travel_ok", True),
        "categories": result.get("categories", {}),
        "recommendation": result.get("recommendation", ""),
        "alert_count": len(alerts),
    }
    db.store_pulse(pulse)
    log.info("Pulse generated: %d alerts, travel_ok=%s", len(alerts), pulse["travel_ok"])
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
            "travel_ok": p["travel_ok"],
            "categories": p["categories"],
        })

    prompt_text = template.format_map({
        "date": date,
        "pulse_count": len(pulses),
        "pulses_json": json.dumps(pulses_for_prompt, ensure_ascii=False, indent=2),
        "previous_summaries": prev_text,
    })

    result = _call_gemini(prompt_config, prompt_text)
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
