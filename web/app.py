import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as http_requests
import yaml
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, send_file, session

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_status_json, init_db

app = Flask(__name__)
app.secret_key = os.getenv("ADMIN_TOKEN", "") or secrets.token_hex(32)

DATA_DIR             = Path(os.getenv("DATA_DIR", "/app/data"))
CONFIG_FILE          = DATA_DIR / "config.yaml"
RADAR_DIR            = DATA_DIR / "radar"
BUILD_VERSION        = os.getenv("BUILD_VERSION", "dev")
MAIN_PY              = Path(os.getenv("MAIN_PY", "/app/main.py"))
POLLER_TRIGGER_URL   = os.getenv("POLLER_TRIGGER_URL", "")
ADMIN_TOKEN          = os.getenv("ADMIN_TOKEN", "")



init_db()


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Server"] = "unknown"
    return response


@app.route("/")
def index():
    web_cfg = _web_config() or {}
    return render_template(
        "index.html",
        allow_poll=_allow_manual_poll(),
        version=BUILD_VERSION,
        telegram_channel_url=web_cfg.get("telegram_channel_url") or "",
        telegram_bot_url=web_cfg.get("telegram_bot_url") or "",
        kofi_url=web_cfg.get("kofi_url") or "",
        sponsor_url=web_cfg.get("sponsor_url") or "",
        github_url=web_cfg.get("github_url") or "",
        legal_url=web_cfg.get("legal_url") or "",
        site_url=(web_cfg.get("site_url") or "").rstrip("/"),
        umami_url=(web_cfg.get("umami_url") or "").rstrip("/"),
        umami_website_id=web_cfg.get("umami_website_id") or "",
        website_disabled_default=web_cfg.get("disabled_default_sources") or [],
        stadia_api_key=os.getenv("STADIA_API_KEY", ""),
    )


@app.route("/alert/<alert_id>")
def alert_detail(alert_id):
    return redirect(f"/?alert={alert_id}")


@app.route("/legal")
def legal():
    web_cfg = _web_config() or {}
    impressum_address = web_cfg.get("impressum_address") or ""
    if not impressum_address:
        abort(404)
    return render_template(
        "legal.html",
        controller=web_cfg.get("operator_name") or "",
        contact=web_cfg.get("operator_contact") or "",
        impressum_address=impressum_address,
    )




@app.route("/robots.txt")
def robots_txt():
    web_cfg = _web_config() or {}
    site_url = (web_cfg.get("site_url") or "").rstrip("/")
    lines = ["User-agent: *", "Allow: /"]
    if site_url:
        lines.append(f"Sitemap: {site_url}/sitemap.xml")
    return Response("\n".join(lines) + "\n", mimetype="text/plain")


@app.route("/api/status")
def api_status():
    return jsonify(get_status_json())


@app.route("/api/poll", methods=["POST"])
def api_poll():
    if not _allow_manual_poll():
        return jsonify({"error": "Manual poll disabled"}), 403
    if POLLER_TRIGGER_URL:
        try:
            resp = http_requests.post(POLLER_TRIGGER_URL, timeout=95)
            if resp.status_code != 200:
                return jsonify({"error": resp.text[-500:]}), resp.status_code
        except http_requests.RequestException as e:
            return jsonify({"error": str(e)}), 502
    else:
        try:
            result = subprocess.run(
                [sys.executable, str(MAIN_PY)],
                capture_output=True,
                text=True,
                timeout=90,
                cwd=str(MAIN_PY.parent),
            )
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Poll timed out after 90s"}), 504
        if result.returncode != 0:
            return jsonify({"error": result.stderr[-500:]}), 500
    return jsonify(get_status_json())


@app.route("/pulse-methodology")
def pulse_methodology():
    return render_template("pulse_methodology.html")


@app.route("/api/pulse-methodology-data")
def api_pulse_methodology_data():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entries = _read_jsonl(DATA_DIR / "pulse_debug" / f"{today}.jsonl")
    if not entries:
        return jsonify({"error": "no data available for today"}), 404
    entry = entries[-1]
    layer1 = entry.get("layer_1_deterministic") or {}
    layer3 = entry.get("layer_3_output") or {}
    timeseries = layer1.get("timeseries") or {}
    categories_out = layer3.get("categories") or {}

    categories = {}
    for cat in ("transport", "weather", "roadworks", "events", "incidents"):
        ts = timeseries.get(cat) or {}
        current = ts.get("current") or {}
        baseline = ts.get("baseline") or {}
        history_raw = ts.get("history") or []
        cat_out = categories_out.get(cat) or {}
        projected = current.get("projected")
        categories[cat] = {
            "status": cat_out.get("status"),
            "trend": cat_out.get("trend"),
            "ongoing": {
                "score": current.get("ongoing", {}).get("score") if current.get("ongoing") else None,
                "count": current.get("ongoing", {}).get("count") if current.get("ongoing") else None,
            },
            "projected": {
                "score": projected.get("score") if projected else None,
                "count": projected.get("count") if projected else None,
            } if projected else None,
            "baseline": {
                "mean": baseline.get("mean"),
                "p75": baseline.get("p75"),
            },
            "history": [h.get("score") if isinstance(h, dict) else h for h in history_raw],
        }

    return jsonify({
        "generated_at": entry.get("generated_at"),
        "alert_count": layer3.get("alert_count"),
        "categories": categories,
    })


@app.route("/radar-test")
def radar_test():
    return render_template("radar_test.html")


@app.route("/api/radar/frames")
def api_radar_frames():
    obs_dir      = RADAR_DIR / "obs"
    forecast_dir = RADAR_DIR / "forecast"
    obs      = sorted(f.stem for f in obs_dir.glob("*.png"))      if obs_dir.exists()      else []
    forecast = sorted(f.stem for f in forecast_dir.glob("*.png")) if forecast_dir.exists() else []
    return jsonify({"obs": obs, "forecast": forecast})


@app.route("/api/radar/obs/<filename>")
def api_radar_obs(filename):
    path = RADAR_DIR / "obs" / filename
    if not path.exists() or path.suffix != ".png":
        abort(404)
    return send_file(path, mimetype="image/png")


@app.route("/api/radar/forecast/<filename>")
def api_radar_forecast(filename):
    path = RADAR_DIR / "forecast" / filename
    if not path.exists() or path.suffix != ".png":
        abort(404)
    return send_file(path, mimetype="image/png")


def _web_config() -> dict | None:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text())
        return cfg.get("web")
    except Exception:
        return None


def _allow_manual_poll() -> bool:
    web = _web_config()
    if web is None:
        return False
    return bool(web.get("allow_manual_poll", False))


# ── Admin dashboard ──────────────────────────────────────────────────────────

def _admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not ADMIN_TOKEN:
            abort(404)
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if not ADMIN_TOKEN:
        abort(404)
    if request.method == "POST":
        if request.form.get("token") == ADMIN_TOKEN:
            session["admin"] = True
            return redirect("/admin")
        return render_template("admin_login.html", error="Invalid token"), 401
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/")


@app.route("/admin")
@_admin_required
def admin_dashboard():
    return render_template("admin.html", version=BUILD_VERSION)


@app.route("/api/admin/data")
@_admin_required
def api_admin_data():
    from db import get_daily_usage, get_monthly_cost, get_hours_with_usage, get_days_with_usage

    date = request.args.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    month = date[:7]
    config = {}
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except Exception:
        pass

    daily = get_daily_usage(date)
    hours_active = get_hours_with_usage(date)
    total_eur, monthly = get_monthly_cost(month, config)
    days_active = get_days_with_usage(month)

    first_of_month = datetime(int(month[:4]), int(month[5:7]), 1)
    prev_month_end = first_of_month - timedelta(days=1)
    prev_month = prev_month_end.strftime("%Y-%m")
    prev_total_eur, _ = get_monthly_cost(prev_month, config)

    translate_debug = _read_jsonl(DATA_DIR / "translate_debug" / f"{date}.jsonl")
    pulse_debug = _read_jsonl(DATA_DIR / "pulse_debug" / f"{date}.jsonl")
    cost_debug = _read_jsonl(DATA_DIR / "cost_debug" / f"{date}.jsonl")

    return jsonify({
        "date": date,
        "month": month,
        "daily_usage": daily,
        "hours_active": hours_active,
        "monthly": {
            "total_eur": round(total_eur, 4),
            "days_active": days_active,
            "services": {s: {"calls": d["calls"], "cost_eur": round(d["cost"], 4)} for s, d in monthly.items()},
        },
        "prev_monthly": {
            "month": prev_month,
            "total_eur": round(prev_total_eur, 4),
        },
        "translate_debug": translate_debug,
        "pulse_debug": pulse_debug,
        "cost_debug": cost_debug,
        "budget": config.get("cost", {}).get("monthly_budget_eur", 10),
        "pricing": _get_pricing(config),
    })


@app.route("/api/admin/weight-review", methods=["POST"])
@_admin_required
def api_admin_weight_review():
    from db import get_status_overrides, get_category_snapshots
    import os, requests as http_req, textwrap
    from pathlib import Path as _Path

    overrides = get_status_overrides(limit=100)
    if not overrides:
        return jsonify({"error": "No overrides recorded yet — add some first"}), 400

    config = {}
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except Exception:
        pass

    prompt_path = _Path(os.getenv("DATA_DIR", "/app/data")) / "prompts" / "weight_review.md"
    if not prompt_path.exists():
        prompt_path = _Path(__file__).parent.parent / "prompts" / "weight_review.md"
    try:
        raw = prompt_path.read_text(encoding="utf-8")
    except OSError:
        return jsonify({"error": "weight_review.md prompt not found"}), 500

    lines = raw.splitlines()
    front_end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    prompt_template = "\n".join(lines[(front_end + 1 if front_end else 0):])

    weight_table = textwrap.dedent("""
        DWD severity: minor=0.5, moderate=1.0, severe=1.5, extreme=2.0
        RMV: S-Bahn/U-Bahn/Regional=1.5, Tram/Bus=0.5, other=1.0
        Autobahn: closure keyword=2.0, else=1.0
        Baustellen: City (Full)=1.5, City (Partial)=0.5, else=1.0
        Events/Messe/Sports: 2.0 (fixed)
        Polizei/Strike: 1.0 (default)
    """).strip()

    pulse_debug_dir = DATA_DIR / "pulse_debug"
    score_breakdowns = []
    seen_ts = set()
    for ov in overrides[:10]:
        ts_date = ov["pulse_ts"][:10]
        if ts_date in seen_ts:
            continue
        seen_ts.add(ts_date)
        jsonl = pulse_debug_dir / f"{ts_date}.jsonl"
        if jsonl.exists():
            for line in jsonl.read_text(encoding="utf-8").splitlines():
                try:
                    entry = json.loads(line)
                    if entry.get("generated_at", "")[:16] == ov["pulse_ts"][:16]:
                        bd = (entry.get("layer_1_deterministic") or {}).get("score_breakdown", {})
                        cat_bd = bd.get(ov["category"], {})
                        score_breakdowns.append({
                            "pulse_ts": ov["pulse_ts"],
                            "category": ov["category"],
                            "override": f"{ov['computed_status']} → {ov['override_status']}",
                            "reason": ov["reason"],
                            "breakdown": cat_bd,
                        })
                        break
                except Exception:
                    pass

    baselines_text = "Not available (requires recent pulse data)"

    prompt_text = prompt_template.format(
        weight_table=weight_table,
        baselines=baselines_text,
        overrides=json.dumps(overrides, indent=2),
        score_breakdowns=json.dumps(score_breakdowns, indent=2),
    )

    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "GEMINI_API_KEY not set"}), 500

    prompt_cfg = {"model": "gemini-2.5-flash", "temperature": 0.2, "response_mime_type": "application/json"}
    model = prompt_cfg["model"]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    try:
        resp = http_req.post(url, params={"key": api_key}, json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][-1]["text"]
        result = json.loads(raw_text)
        return jsonify({"result": result, "overrides_analyzed": len(overrides)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _get_pricing(config: dict) -> dict:
    cost_cfg = config.get("cost", {})
    gemini = cost_cfg.get("gemini", {})
    translate = cost_cfg.get("google_translate", {})
    return {
        "usd_to_eur": cost_cfg.get("usd_to_eur", 0.92),
        "gemini_input_per_m": gemini.get("input_per_million", 0.15),
        "gemini_output_per_m": gemini.get("output_per_million", 0.60),
        "gemini_thinking_per_m": gemini.get("thinking_per_million", 3.50),
        "translate_chars_per_m": translate.get("chars_per_million", 20.0),
    }


@app.route("/api/admin/cost-history")
@_admin_required
def api_admin_cost_history():
    from db import get_daily_usage, get_monthly_cost

    days = int(request.args.get("days", 7))
    config = {}
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    except Exception:
        pass

    today = datetime.now(timezone.utc)
    history = []
    for i in range(days - 1, -1, -1):
        from datetime import timedelta
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        usage = get_daily_usage(d)
        gemini_cost = 0.0
        translate_cost = 0.0
        for row in usage:
            svc = row["service"]
            if svc.startswith("gemini_"):
                cost_cfg = config.get("cost", {}).get("gemini", {})
                usd_to_eur = config.get("cost", {}).get("usd_to_eur", 0.92)
                cost_usd = (row["tokens_in"] / 1e6 * cost_cfg.get("input_per_million", 0.15)
                            + row["tokens_out"] / 1e6 * cost_cfg.get("output_per_million", 0.60)
                            + row["tokens_thinking"] / 1e6 * cost_cfg.get("thinking_per_million", 3.50))
                gemini_cost += cost_usd * usd_to_eur
            elif svc == "google_translate":
                cost_cfg = config.get("cost", {}).get("google_translate", {})
                usd_to_eur = config.get("cost", {}).get("usd_to_eur", 0.92)
                cost_usd = row["characters"] / 1e6 * cost_cfg.get("chars_per_million", 20.0)
                translate_cost += cost_usd * usd_to_eur
        history.append({
            "date": d,
            "gemini_eur": round(gemini_cost, 4),
            "translate_eur": round(translate_cost, 4),
        })

    return jsonify({"history": history, "budget": config.get("cost", {}).get("monthly_budget_eur", 10)})


@app.route("/api/admin/server-status")
@_admin_required
def api_admin_server_status():
    from db import get_meta, get_subscriber_counts

    health_raw = get_meta("admin_health")
    health = json.loads(health_raw) if health_raw else {}
    last_polled = get_meta("last_polled_at")
    counts = get_subscriber_counts()

    label_map = {
        "translator": "Translator", "poll_schedule": "Cron",
    }
    components = {
        label_map.get(k, k.replace("Poller", "")): v
        for k, v in health.items()
    }

    return jsonify({
        "components": components,
        "last_polled": last_polled,
        "subscribers": counts,
    })


@app.route("/api/admin/poll", methods=["POST"])
@_admin_required
def api_admin_poll():
    if not POLLER_TRIGGER_URL:
        return jsonify({"error": "POLLER_TRIGGER_URL not configured"}), 500
    try:
        resp = http_requests.post(POLLER_TRIGGER_URL, timeout=95)
        if resp.status_code != 200:
            return jsonify({"error": resp.text[-500:]}), resp.status_code
        return jsonify({"status": "ok"})
    except http_requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/admin/pulse", methods=["POST"])
@_admin_required
def api_admin_pulse():
    if not POLLER_TRIGGER_URL:
        return jsonify({"error": "POLLER_TRIGGER_URL not configured"}), 500
    pulse_url = POLLER_TRIGGER_URL.rsplit("/", 1)[0] + "/pulse"
    try:
        resp = http_requests.post(pulse_url, timeout=65)
        if resp.status_code != 200:
            return jsonify({"error": resp.text[-500:]}), resp.status_code
        return jsonify({"status": "ok"})
    except http_requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/admin/overrides")
@_admin_required
def api_admin_overrides():
    from db import get_status_overrides
    return jsonify({"overrides": get_status_overrides()})


@app.route("/api/admin/overrides", methods=["POST"])
@_admin_required
def api_admin_override_add():
    from db import add_status_override
    data = request.get_json(silent=True) or {}
    pulse_ts = data.get("pulse_ts", "")
    category = data.get("category", "")
    computed_status = data.get("computed_status", "")
    override_status = data.get("override_status", "")
    reason = data.get("reason", "").strip()
    if not all([pulse_ts, category, computed_status, override_status, reason]):
        return jsonify({"error": "All fields required"}), 400
    overrides = add_status_override(pulse_ts, category, computed_status, override_status, reason)
    return jsonify({"overrides": overrides})


@app.route("/api/admin/overrides/<int:override_id>", methods=["DELETE"])
@_admin_required
def api_admin_override_delete(override_id):
    from db import delete_status_override
    delete_status_override(override_id)
    from db import get_status_overrides
    return jsonify({"overrides": get_status_overrides()})


@app.route("/api/admin/bans")
@_admin_required
def api_admin_bans():
    from db import get_meta
    raw = get_meta("banned_chat_ids")
    banned = json.loads(raw) if raw else []
    return jsonify({"banned": sorted(banned)})


@app.route("/api/admin/ban", methods=["POST"])
@_admin_required
def api_admin_ban():
    from db import get_meta, set_meta
    data = request.get_json(silent=True) or {}
    try:
        chat_id = int(data.get("chat_id", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid chat_id"}), 400
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    raw = get_meta("banned_chat_ids")
    banned = set(json.loads(raw)) if raw else set()
    if chat_id in banned:
        return jsonify({"error": f"{chat_id} is already banned"}), 409
    banned.add(chat_id)
    set_meta("banned_chat_ids", json.dumps(sorted(banned)))
    return jsonify({"status": "ok", "banned": sorted(banned)})


@app.route("/api/admin/unban", methods=["POST"])
@_admin_required
def api_admin_unban():
    from db import get_meta, set_meta
    data = request.get_json(silent=True) or {}
    try:
        chat_id = int(data.get("chat_id", 0))
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid chat_id"}), 400
    if not chat_id:
        return jsonify({"error": "chat_id required"}), 400

    raw = get_meta("banned_chat_ids")
    banned = set(json.loads(raw)) if raw else set()
    if chat_id not in banned:
        return jsonify({"error": f"{chat_id} is not banned"}), 404
    banned.discard(chat_id)
    set_meta("banned_chat_ids", json.dumps(sorted(banned)))
    return jsonify({"status": "ok", "banned": sorted(banned)})


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    except (OSError, json.JSONDecodeError):
        pass
    return entries


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not found"}), 404
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000)
