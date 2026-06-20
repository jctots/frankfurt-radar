import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)

_health = {"ok": True}

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

_KEY_RE = re.compile(r"key=[A-Za-z0-9._-]+")


def _mask_key(text: str) -> str:
    return _KEY_RE.sub("key=***", text)


def extraction_ok() -> bool:
    return _health["ok"]


def reset_extraction_health() -> None:
    _health["ok"] = True


def extract_alert_details(text: str, prompt: str) -> dict:
    """Send *text* to Gemini Flash with *prompt* and return parsed JSON.

    The prompt must instruct the model to respond with a JSON object.
    Returns ``{}`` on any failure (missing key, network, bad JSON).
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        log.warning("GEMINI_API_KEY not set — extraction skipped")
        return {}

    body = {
        "contents": [{"parts": [{"text": f"{prompt}\n\n---\n\n{text}"}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
        },
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                _GEMINI_URL,
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
            candidates = resp.json().get("candidates", [])
            if not candidates:
                log.error("Gemini returned no candidates")
                _health["ok"] = False
                return {}
            raw = candidates[0]["content"]["parts"][0]["text"]
            return json.loads(raw)
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


STRIKE_EXTRACTION_PROMPT = """\
You are analyzing a German press release about a labor strike or warning strike (Warnstreik).
Extract the following fields and respond with a JSON object:

{
  "summary": "2-3 sentence English summary of the strike: who is striking, what sector, when, where, and why",
  "valid_from": "Strike start date/time in ISO 8601 format with Europe/Berlin timezone (e.g. 2026-06-05T00:00:00+02:00), or null if not determinable",
  "valid_until": "Strike end date/time in ISO 8601 format with Europe/Berlin timezone, or null if not determinable. For open-ended strikes use null.",
  "location": "Specific rally/demo location if mentioned (e.g. 'Hauptwache, Frankfurt'), otherwise the city or region name (e.g. 'Frankfurt und Region')",
  "service": "One of: Transport, Retail, Public Sector, Aviation, Healthcare, Other",
  "affected": ["List of affected companies or institutions, e.g. 'VGF', 'Rewe', 'Goethe-Universität'"]
}

Rules:
- All date/times must use Europe/Berlin timezone offset (+01:00 or +02:00 depending on DST).
- If the strike spans multiple days, valid_from is the start of the first day, valid_until is the end of the last day.
- For single-day strikes without specific end time, set valid_until to end of day (23:59).
- If the press release is about negotiations or general union news (not an actual strike call), return {"not_a_strike": true}.
- The summary must be in English.\
"""
