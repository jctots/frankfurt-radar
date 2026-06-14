#!/usr/bin/env python3
"""Standalone radar image poller — downloads DWD WMS frames to DATA_DIR/radar/.

Run every 10 minutes alongside main.py (added to crontab by entrypoint.sh).

Layout written:
  DATA_DIR/radar/obs/       — up to OBS_COUNT rolling observation PNGs
  DATA_DIR/radar/forecast/  — FC_COUNT forecast PNGs, replaced each run

Constants must stay in sync with radar_test.html (IMAGE_BOUNDS, OBS_COUNT, FC_COUNT,
MIN_FRAME_BYTES) and the web API routes in web/app.py.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

DATA_DIR  = Path(os.getenv("DATA_DIR", "data"))
RADAR_DIR = DATA_DIR / "radar"
OBS_DIR   = RADAR_DIR / "obs"
FC_DIR    = RADAR_DIR / "forecast"

_DWD_WMS         = "https://maps.dwd.de/geoserver/wms"
_LAYER           = "dwd:Radar_wn-product_1x1km_ger"
_WMS_BBOX        = "667917,6106854,1269042,6837402"
_IMG_W, _IMG_H   = 800, 600
OBS_COUNT        = 18
FC_COUNT         = 12
MIN_FRAME_BYTES  = 2000
_REQUEST_TIMEOUT = 15


def _ts(dt: datetime) -> str:
    """YYYYMMDDTHHmmZ — must match radar_test.html dateToTs()."""
    return dt.strftime("%Y%m%dT%H%MZ")


def _wms_url(dt: datetime) -> str:
    iso = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return (
        f"{_DWD_WMS}?SERVICE=WMS&VERSION=1.1.1&REQUEST=GetMap"
        f"&LAYERS=dwd%3ARadar_wn-product_1x1km_ger&STYLES="
        f"&FORMAT=image%2Fpng&TRANSPARENT=true"
        f"&SRS=EPSG%3A3857&BBOX={_WMS_BBOX}&WIDTH={_IMG_W}&HEIGHT={_IMG_H}"
        f"&TIME={iso.replace(':', '%3A')}"
    )


def _fetch(dt: datetime) -> bytes | None:
    """Fetch one WMS frame; returns bytes if ≥ MIN_FRAME_BYTES, else None."""
    url = _wms_url(dt)
    try:
        r = requests.get(url, timeout=_REQUEST_TIMEOUT)
        r.raise_for_status()
        if len(r.content) < MIN_FRAME_BYTES:
            log.debug("frame %s too small (%d bytes)", _ts(dt), len(r.content))
            return None
        return r.content
    except Exception as exc:
        log.warning("radar fetch failed %s: %s", _ts(dt), exc)
        return None


def _anchor() -> datetime:
    """T-10min: latest obs frame timestamp (T = current 10-min boundary)."""
    now = datetime.now(timezone.utc)
    t = now.replace(second=0, microsecond=0)
    t -= timedelta(minutes=t.minute % 10)
    return t - timedelta(minutes=10)


def _obs_timestamps(anchor: datetime) -> list[datetime]:
    return [anchor - timedelta(minutes=(OBS_COUNT - 1 - i) * 10) for i in range(OBS_COUNT)]


def _fc_timestamps(anchor: datetime) -> list[datetime]:
    return [anchor + timedelta(minutes=(i + 1) * 10) for i in range(FC_COUNT)]


def _update_obs(anchor: datetime) -> None:
    """Add latest obs frame; fall back to T-20min if anchor is unavailable; prune to OBS_COUNT."""
    OBS_DIR.mkdir(parents=True, exist_ok=True)

    path = OBS_DIR / f"{_ts(anchor)}.png"
    if not path.exists():
        data = _fetch(anchor)
        if data is None:
            fallback = anchor - timedelta(minutes=10)
            log.info("obs anchor unavailable, trying T-20min fallback %s", _ts(fallback))
            data = _fetch(fallback)
            if data:
                fb_path = OBS_DIR / f"{_ts(fallback)}.png"
                if not fb_path.exists():
                    fb_path.write_bytes(data)
                    log.info("obs fallback saved %s", fb_path.name)
        else:
            path.write_bytes(data)
            log.info("obs saved %s", path.name)

    frames = sorted(OBS_DIR.glob("*.png"))
    for old in frames[:-OBS_COUNT]:
        old.unlink()
        log.info("obs pruned %s", old.name)


def _backfill_obs(anchor: datetime) -> None:
    """Cold-start: download all OBS_COUNT historical frames."""
    OBS_DIR.mkdir(parents=True, exist_ok=True)
    for t in _obs_timestamps(anchor):
        path = OBS_DIR / f"{_ts(t)}.png"
        if path.exists():
            continue
        data = _fetch(t)
        if data:
            path.write_bytes(data)
            log.info("obs backfill saved %s", path.name)
        else:
            log.debug("obs backfill no data for %s", _ts(t))


def _update_forecast(anchor: datetime) -> None:
    """Replace all forecast frames for the current run."""
    FC_DIR.mkdir(parents=True, exist_ok=True)

    new: dict[str, bytes] = {}
    for t in _fc_timestamps(anchor):
        data = _fetch(t)
        if data:
            new[_ts(t)] = data

    for old in FC_DIR.glob("*.png"):
        old.unlink()

    for ts, data in new.items():
        (FC_DIR / f"{ts}.png").write_bytes(data)
        log.info("forecast saved %s", ts)

    log.info("forecast update: %d/%d frames", len(new), FC_COUNT)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    anchor = _anchor()
    log.info("radar poll anchor=%s", _ts(anchor))

    existing = list(OBS_DIR.glob("*.png")) if OBS_DIR.exists() else []
    if len(existing) < OBS_COUNT:
        log.info("cold-start backfill (%d existing obs frames)", len(existing))
        _backfill_obs(anchor)
    else:
        _update_obs(anchor)

    _update_forecast(anchor)
    log.info("radar poll complete")


if __name__ == "__main__":
    main()
