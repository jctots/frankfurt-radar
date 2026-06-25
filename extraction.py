import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_health = {"ok": True}

_GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash:generateContent"
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
            data = resp.json()
            usage = data.get("usageMetadata", {})
            if usage:
                from db import record_api_usage
                record_api_usage(
                    "gemini_extraction",
                    tokens_in=usage.get("promptTokenCount", 0),
                    tokens_out=usage.get("candidatesTokenCount", 0)
                        + usage.get("thoughtsTokenCount", 0),
                )
            candidates = data.get("candidates", [])
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


def strike_extraction_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Today's date is {today}.\n\n"
        "You are analyzing a German press release about a labor strike or warning strike (Warnstreik).\n"
        "Extract the following fields and respond with a JSON object:\n\n"
        "{\n"
        '  "summary": "2-3 sentence English summary of the strike: who is striking, what sector, when, where, and why",\n'
        '  "valid_from": "Strike start date/time in ISO 8601 format with Europe/Berlin timezone (e.g. 2026-06-05T00:00:00+02:00), or null if not determinable",\n'
        '  "valid_until": "Strike end date/time in ISO 8601 format with Europe/Berlin timezone, or null if not determinable. For open-ended strikes use null.",\n'
        '  "location": "Specific rally/demo location if mentioned (e.g. \'Hauptwache, Frankfurt\'), otherwise the city or region name (e.g. \'Frankfurt und Region\')",\n'
        '  "lat": "Latitude of the location as a decimal number (e.g. 50.1009), or null if region-wide or unknown",\n'
        '  "lon": "Longitude of the location as a decimal number (e.g. 8.6821), or null if region-wide or unknown",\n'
        '  "service": "One of: Transport, Retail, Public Sector, Aviation, Healthcare, Other",\n'
        '  "affected": ["List of affected companies or institutions, e.g. \'VGF\', \'Rewe\', \'Goethe-Universität\'"]\n'
        "}\n\n"
        "Rules:\n"
        "- All date/times must use Europe/Berlin timezone offset (+01:00 or +02:00 depending on DST).\n"
        "- Dates must be consistent with the press release's publication date. Do not output dates from prior years.\n"
        "- If the strike spans multiple days, valid_from is the start of the first day, valid_until is the end of the last day.\n"
        "- For single-day strikes without specific end time, set valid_until to end of day (23:59).\n"
        '- If the press release is about negotiations or general union news (not an actual strike call), return {"not_a_strike": true}.\n'
        "- The summary must be in English.\n"
        "- For coordinates, use the approximate location of the place mentioned (street, intersection, landmark, station). "
        "If only a district is mentioned, use the district centre. If the strike is region-wide, return null for lat and lon."
    )


def police_location_prompt() -> str:
    return (
        "You are extracting the location from a German police report (Polizeimeldung) "
        "in the Frankfurt am Main area.\n\n"
        "Extract and respond with a JSON object:\n\n"
        "{\n"
        '  "location": "Place and district where the incident occurred '
        "(e.g. 'Schweizer Platz, Sachsenhausen', 'Hauptbahnhof, Bahnhofsviertel', "
        "'Berger Strasse, Bornheim'). Use original German place names. "
        'If the location cannot be determined, use null.",\n'
        '  "lat": "Latitude as a decimal number (e.g. 50.1009), or null if unknown",\n'
        '  "lon": "Longitude as a decimal number (e.g. 8.6821), or null if unknown"\n'
        "}\n\n"
        "Rules:\n"
        "- Use well-known Frankfurt districts (Stadtteile): Sachsenhausen, Bornheim, "
        "Innenstadt, Nordend, Westend, Bockenheim, Gallus, Ostend, Bahnhofsviertel, "
        "Altstadt, Hoechst, Niederrad, Griesheim, Fechenheim, etc.\n"
        "- For coordinates, use the approximate location of the place mentioned. "
        "If only a district is mentioned, use the district centre.\n"
        "- Do not guess or hallucinate locations not mentioned in the text."
    )



STRIKE_DEDUP_PROMPT = """\
Are these two alerts about the same labor strike or warning strike event?

EXISTING ALERT:
Title: {existing_title}
Summary: {existing_body}
From: {existing_from}
Until: {existing_until}
Service: {existing_service}

NEW ALERT:
Title: {new_title}
Summary: {new_body}
From: {new_from}
Until: {new_until}
Service: {new_service}

Respond with a JSON object: {{"same_event": true}} or {{"same_event": false}}.
Only return true if both alerts clearly describe the same strike action by the same union affecting the same workers/companies.\
"""
