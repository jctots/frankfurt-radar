"""Frankfurt Radar MCP Server — read-only alert access for AI assistants."""

import json
import logging
import os
from collections import Counter

from mcp.server.fastmcp import FastMCP

from auth import ApiKeyAuthMiddleware, is_admin_request, _track_mcp_call  # noqa: F401 (request_key_id set by middleware)
import db
import models

log = logging.getLogger(__name__)

port = int(os.getenv("MCP_PORT", "8811"))

mcp = FastMCP(
    "Frankfurt Radar",
    instructions="Real-time Frankfurt alerts: transit, weather, police, roads, events",
    host="0.0.0.0",
    port=port,
)

VALID_SOURCES = list(models.SOURCE_LABEL.keys())


def _format_alert(row: dict) -> dict:
    title, body = models.format_alert_message(row)
    return {
        "id": row["alert_id"],
        "source": row["source"],
        "source_label": models.SOURCE_LABEL.get(row["source"], row["source"]),
        "title": title,
        "body": body,
        "severity": row.get("severity"),
        "service": row.get("service"),
        "lines": json.loads(row["lines"]) if row.get("lines") else [],
        "location": row.get("location_label"),
        "url": row.get("url"),
        "valid_from": row.get("valid_from"),
        "valid_until": row.get("valid_until"),
        "published_at": row.get("published_at"),
        "stale": bool(row.get("stale")),
    }


def _track(tool_name: str) -> None:
    if not is_admin_request.get(False):
        _track_mcp_call(tool_name)


@mcp.tool()
def get_active_alerts(source: str | None = None) -> list[dict]:
    """List all active alerts for Frankfurt, optionally filtered by source.

    Sources: rmv, dwd, polizei, autobahn, baustellen, events, sports
    """
    _track("get_active_alerts")
    alerts = db.get_all_active_alerts()
    if source:
        source = source.lower()
        alerts = [a for a in alerts if a["source"] == source]
    return [_format_alert(a) for a in alerts]


@mcp.tool()
def search_alerts(query: str) -> list[dict]:
    """Search active alerts by keyword. Multiple words use AND matching
    across title, body, service, and location fields."""
    _track("search_alerts")
    results = db.search_active_alerts(query)
    return [_format_alert(a) for a in results]


@mcp.tool()
def get_alert_details(alert_id: str) -> dict | None:
    """Get full details for a single alert by its ID."""
    _track("get_alert_details")
    with db._conn() as conn:
        row = conn.execute(
            "SELECT * FROM alert_cache WHERE alert_id = ? AND removed_at IS NULL",
            (alert_id,),
        ).fetchone()
    if not row:
        return None
    return _format_alert(dict(row))


@mcp.tool()
def get_system_status() -> dict:
    """System health: last poll time, source status, and active alert counts."""
    _track("get_system_status")
    status = db.get_status_json()
    by_source = Counter(a["source"] for a in status["alerts"])
    return {
        "last_polled_at": status["updated_at"],
        "total_active_alerts": len(status["alerts"]),
        "total_recently_removed": len(status["removed_alerts"]),
        "alerts_by_source": dict(by_source),
        "source_health": status["source_health"],
    }


@mcp.tool()
def get_alert_stats() -> dict:
    """Summary statistics: count by source, by severity, oldest/newest alert."""
    _track("get_alert_stats")
    alerts = db.get_all_active_alerts()
    by_source = Counter(a["source"] for a in alerts)
    by_severity = Counter(a["severity"] for a in alerts if a.get("severity"))
    timestamps = [a["cached_at"] for a in alerts if a.get("cached_at")]
    return {
        "total_active": len(alerts),
        "by_source": {
            src: {"count": by_source.get(src, 0), "label": label}
            for src, label in models.SOURCE_LABEL.items()
        },
        "by_severity": dict(by_severity),
        "oldest_cached": min(timestamps) if timestamps else None,
        "newest_cached": max(timestamps) if timestamps else None,
    }


if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Mount
    from starlette.applications import Starlette
    from starlette.middleware import Middleware

    sse_app = mcp.sse_app()
    app = Starlette(
        routes=[Mount("/", app=sse_app)],
        middleware=[Middleware(ApiKeyAuthMiddleware)],
    )
    uvicorn.run(app, host="0.0.0.0", port=port)
