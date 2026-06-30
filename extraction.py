import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

_GEMINI_DEBUG_DIR = Path(os.getenv("DATA_DIR", ".")) / "pulse_debug"

_health = {"ok": True}

_GEMINI_URL_TPL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "{model}:generateContent"
)

_KEY_RE = re.compile(r"key=[A-Za-z0-9._-]+")


def _mask_key(text: str) -> str:
    return _KEY_RE.sub("key=***", text)


def extraction_ok() -> bool:
    return _health["ok"]


def reset_extraction_health() -> None:
    _health["ok"] = True


def extract_alert_details(text: str, prompt: str, prompt_config: dict | None = None, extraction_type: str = "") -> dict:
    """Send *text* to Gemini Flash with *prompt* and return parsed JSON.

    The prompt must instruct the model to respond with a JSON object.
    Returns ``{}`` on any failure (missing key, network, bad JSON).
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — extraction skipped")
        return {}

    if prompt_config is None:
        prompt_config = {}

    model = prompt_config.get("model", "gemini-2.5-flash")
    url = _GEMINI_URL_TPL.format(model=model)

    gen_config = {
        "responseMimeType": prompt_config.get("response_mime_type", "application/json"),
        "temperature": prompt_config.get("temperature", 0.1),
    }
    if "max_output_tokens" in prompt_config:
        gen_config["maxOutputTokens"] = prompt_config["max_output_tokens"]
    if "thinking_budget" in prompt_config:
        gen_config["thinkingConfig"] = {"thinkingBudget": prompt_config["thinking_budget"]}

    combined = f"{prompt}\n\n---\n\n{text}" if text else prompt
    body = {
        "contents": [{"parts": [{"text": combined}]}],
        "generationConfig": gen_config,
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                params={"key": api_key},
                json=body,
                timeout=30,
            )
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
                from db import record_api_usage
                record_api_usage("gemini_extraction", **usage)
            candidates = data.get("candidates", [])
            if not candidates:
                log.error("Gemini returned no candidates")
                _health["ok"] = False
                return {}
            raw = candidates[0]["content"]["parts"][0]["text"]
            result = json.loads(raw)
            _write_extraction_debug(usage, prompt_config, extraction_type)
            return result
        except requests.RequestException as e:
            log.error("Gemini API request failed: %s", _mask_key(str(e)))
            _health["ok"] = False
            return {}
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            log.error("Gemini response parse failed: %s", e)
            _health["ok"] = False
            return {}

    log.error("Gemini rate limited — all retries exhausted")
    _health["ok"] = False
    return {}


def _write_extraction_debug(usage: dict, prompt_config: dict, extraction_type: str = "") -> None:
    try:
        _GEMINI_DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path = _GEMINI_DEBUG_DIR / f"{today}.jsonl"
        entry = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "service": "gemini_extraction",
            "extraction_type": extraction_type,
            "usage": usage,
            "model": prompt_config.get("model", "gemini-2.5-flash"),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("Extraction debug log write failed: %s", e)


def _load_extraction_prompt(name: str, **kwargs: str) -> tuple[dict, str]:
    from pulse import load_prompt
    config, template = load_prompt(name)
    if kwargs:
        template = template.format(**kwargs)
    return config, template


def strike_extraction_prompt() -> tuple[dict, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _load_extraction_prompt("strike_extraction", today=today)


def police_location_prompt() -> tuple[dict, str]:
    return _load_extraction_prompt("police_location")


def strike_dedup_prompt(**kwargs: str) -> tuple[dict, str]:
    return _load_extraction_prompt("strike_dedup", **kwargs)
