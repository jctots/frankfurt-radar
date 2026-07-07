"""Stage 2 of the City Pulse review pipeline (docs/review.md) — the Gemini
reviewer. Reads the digest built by review.reduce, reasons over it, and
writes a human-readable report plus machine-readable proposed edits to the
data volume. Never edits the running system; `changes.json` only becomes a
draft PR once CI (Phase 4) picks it up.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from pulse import _call_gemini, load_prompt

log = logging.getLogger(__name__)


def _digest_dir() -> Path:
    return Path(os.getenv("DATA_DIR", ".")) / "review_debug"


def run(digest: dict, *, days: int | None = None) -> dict:
    """Run the reviewer over `digest` and write its outputs to the data volume.

    Returns {timestamp, digest_path, report_path, changes_path, report_md,
    changes, copy_paste_prompts, usage}. On any failure (missing API key,
    malformed response) returns the same shape with empty report/changes
    and `usage == {}` — the caller decides how to surface that to the admin.
    """
    days = days if days is not None else digest.get("params", {}).get("days", 7)
    prompt_config, template = load_prompt("review")
    prompt_text = template.format_map({
        "days": days,
        "digest_json": json.dumps(digest, ensure_ascii=False, indent=2),
    })

    result, usage = _call_gemini(prompt_config, prompt_text, service="gemini_review")

    report_md = result.get("report_md", "") if result else ""
    changes = result.get("changes", []) if result else []
    copy_paste_prompts = result.get("copy_paste_prompts", []) if result else []
    if not isinstance(changes, list):
        changes = []
    if not isinstance(copy_paste_prompts, list):
        copy_paste_prompts = []

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H%M%SZ")

    digest_path, report_path, changes_path = _write_outputs(
        timestamp, digest, report_md, changes, copy_paste_prompts, usage
    )

    log.info("Review run %s: %d proposed change(s), %d copy-paste prompt(s)",
              timestamp, len(changes), len(copy_paste_prompts))

    return {
        "timestamp": timestamp,
        "digest_path": str(digest_path),
        "report_path": str(report_path),
        "changes_path": str(changes_path),
        "report_md": report_md,
        "changes": changes,
        "copy_paste_prompts": copy_paste_prompts,
        "usage": usage,
    }


def _write_outputs(
    timestamp: str,
    digest: dict,
    report_md: str,
    changes: list[dict],
    copy_paste_prompts: list[str],
    usage: dict,
) -> tuple[Path, Path, Path]:
    out_dir = _digest_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    digest_path = out_dir / f"{timestamp}.digest.json"
    report_path = out_dir / f"{timestamp}.report.md"
    changes_path = out_dir / f"{timestamp}.changes.json"

    digest_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

    report_text = report_md
    if copy_paste_prompts:
        report_text += "\n\n## Copy-paste prompts\n\n"
        for i, prompt in enumerate(copy_paste_prompts, 1):
            report_text += f"### Prompt {i}\n\n```\n{prompt}\n```\n\n"
    report_path.write_text(report_text, encoding="utf-8")

    changes_path.write_text(
        json.dumps({
            "timestamp": timestamp,
            "config_versions": digest.get("config_versions", []),
            "changes": changes,
            "usage": usage,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return digest_path, report_path, changes_path


def list_reports() -> list[dict]:
    """List past review reports on the data volume, newest first."""
    out_dir = _digest_dir()
    if not out_dir.exists():
        return []
    reports = []
    for report_path in out_dir.glob("*.report.md"):
        timestamp = report_path.name.removesuffix(".report.md")
        reports.append({
            "timestamp": timestamp,
            "report_path": str(report_path),
            "changes_path": str(out_dir / f"{timestamp}.changes.json"),
            "digest_path": str(out_dir / f"{timestamp}.digest.json"),
        })
    reports.sort(key=lambda r: r["timestamp"], reverse=True)
    return reports


def _timestamp_to_iso(timestamp: str) -> str:
    """'2026-07-07T120503Z' -> '2026-07-07T12:05:03Z'."""
    return datetime.strptime(timestamp, "%Y-%m-%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")


def list_reports_for_date(date: str) -> list[dict]:
    """Review runs on `date` (YYYY-MM-DD), shaped like a pulse_debug entry so
    the admin Gemini Log panel can show them alongside pulse/daily/extraction
    calls (see web/app.py's api_admin_data)."""
    entries = []
    for report in list_reports():
        if not report["timestamp"].startswith(date):
            continue
        try:
            changes_data = json.loads(Path(report["changes_path"]).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries.append({
            "generated_at": _timestamp_to_iso(report["timestamp"]),
            "service": "gemini_review",
            "usage": changes_data.get("usage", {}),
            "changes_count": len(changes_data.get("changes", [])),
        })
    return entries
