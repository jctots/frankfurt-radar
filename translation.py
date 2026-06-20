import logging
import os
from typing import TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from models import Alert

log = logging.getLogger(__name__)

_health = {"ok": True}


def translation_ok() -> bool:
    return _health["ok"]


def reset_translation_health() -> None:
    _health["ok"] = True


_UMLAUT_MAP = str.maketrans({
    'ä': 'ae', 'ö': 'oe', 'ü': 'ue',
    'Ä': 'Ae', 'Ö': 'Oe', 'Ü': 'Ue',
    'ß': 'ss',
})


def translate(text: str, config: dict) -> str:
    backend = config.get("translator", {}).get("backend", "libretranslate").lower()
    if backend == "libretranslate":
        return _translate_libre(text, config)
    if backend == "google":
        return _translate_google(text)
    log.warning("Unknown translator backend '%s'; returning original text", backend)
    return text


def _translate_libre(text: str, config: dict) -> str:
    url = config.get("translator", {}).get("libretranslate_url", "http://libretranslate:5000")
    try:
        resp = requests.post(
            f"{url}/translate",
            json={"q": text, "source": "de", "target": "en", "format": "text"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json().get("translatedText", text)
    except requests.RequestException as e:
        _health["ok"] = False
        log.error("LibreTranslate failed: %s", e)
        return text


def _translate_google(text: str) -> str:
    api_key = os.getenv("GOOGLE_TRANSLATE_API_KEY", "")
    if not api_key:
        log.error("GOOGLE_TRANSLATE_API_KEY not set")
        return text
    try:
        resp = requests.post(
            "https://translation.googleapis.com/language/translate/v2",
            params={"key": api_key},
            json={"q": text, "source": "de", "target": "en", "format": "text"},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["data"]["translations"][0]["translatedText"]
    except (requests.RequestException, KeyError) as e:
        _health["ok"] = False
        log.error("Google Translate failed: %s", e)
        return text


def _transliterate(text: str) -> str:
    return text.translate(_UMLAUT_MAP)


def translate_alert(alert: "Alert", config: dict) -> tuple[str, str]:
    if alert.source in ("dwd", "events"):
        return alert.title, alert.body
    if alert.source == "strike":
        en_title = _transliterate(translate(alert.title, config))
        return en_title, alert.body
    if alert.source == "baustellen":
        en_title = _transliterate(alert.title)  # title already in English
        en_body  = _transliterate(translate(alert.body, config)) if alert.body else ""
        return en_title, en_body
    if alert.source == "polizei" and ":" in alert.title:
        location, _, event = alert.title.partition(":")
        en_event = _transliterate(translate(event.strip(), config))
        en_title = f"{_transliterate(location)}: {en_event}"
    else:
        en_title = _transliterate(translate(alert.title, config))
    en_body = _transliterate(translate(alert.body, config)) if alert.body else ""
    return en_title, en_body
