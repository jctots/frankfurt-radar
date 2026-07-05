# City Pulse — Analysis Approach

## Goal

City Pulse provides glanceable situational awareness for Frankfurt. It does not repeat alert titles — users already see those in the feed. Instead, it synthesizes alerts into actionable intelligence: what's the real impact, what correlates across sources, and what should someone do differently.

## Data sources

Frankfurt Radar collects real-time data from 10 sources:

| Source | Category | What it provides |
|--------|----------|------------------|
| RMV (rmv) | Transport | Transit disruptions: S-Bahn, U-Bahn, tram, bus, regional |
| DWD (dwd) | Weather | Severe weather warnings with severity levels |
| Polizei (polizei) | Incidents | Police reports: accidents, closures, events |
| Feuerwehr (feuerwehr) | Incidents | Fire department active incidents by district |
| Strike (strike) | Incidents | Strike alerts extracted from press releases |
| Autobahn (autobahn) | Roadworks | Federal road construction and closures |
| Baustellen (baustellen) | Roadworks | City road construction |
| Events (events) | Events | City festivals: concerts, parades, markets |
| Messe (messe) | Events | Trade fairs at Messe Frankfurt |
| Sports (sports) | Events | Match days, sports events |

The pollers run on a cron schedule (default: every 10 minutes). Alerts are cached in `alert_cache` with timestamps, severity, geolocation, and staleness flags.

## Analysis pipeline

City Pulse processes data through three layers on each hourly run.

### Layer 1 — Deterministic scoring

**Module:** `pulse_categories.py`

Computes severity-weighted scores, stores hourly snapshots, and derives the status label for each category. Everything here is deterministic — no LLM involved.

**Alert classification** — Alerts are placed into buckets:

- **Ongoing**: `valid_from ≤ now` AND (`valid_until ≥ now` OR absent) → active disruption
- **Upcoming**: `valid_from > now` AND within the category's lookahead window → imminent
- **Excluded**: expired alerts or alerts beyond the lookahead window
- **No-temporal sources** (polizei, strike — RSS feeds): always counted as ongoing. Feuerwehr alerts carry a TTL-based `valid_until`, so they are classified normally.

**Severity weighting** — Each alert contributes a weight derived from its fields:

| Source | Field | Weight mapping |
|--------|-------|----------------|
| DWD | `severity` (1–4) | minor=0.5, moderate=1.0, severe=1.5, extreme=2.0 |
| RMV | `service` + `lines` | S-Bahn/U-Bahn/Regional=1.5, Tram=1.0, Bus=0.5 — multiplied by affected line count (capped at 4) |
| Autobahn | `title_en` keyword | "closure"=2.0, else 1.0 |
| Baustellen | `service` | "City (Full)"=1.5, "City (Partial)"=0.5 |
| Events/Messe/Sports | — | Fixed 2.0 |
| Strike | — | Fixed 1.5 |
| Feuerwehr | — | Fixed 1.0 |
| Polizei | — | Fixed 0.5 |

**Hourly snapshots** — Stored in `category_snapshots` (one row per category per hour):

- `ongoing_score`: severity-weighted sum of active alerts
- `projected_score`: estimated score at the end of the next sample interval — `ongoing_score − expiring_near + starting_near`. Only *scheduled* starts and expiries move this; for categories whose alerts aren't scheduled (transport, incidents) it usually equals `ongoing_score`.
- `scheduled_upcoming_score`: severity-weighted sum of alerts with a future `valid_from` inside the lookahead window — pure upcoming activity (storm warnings, planned closures, events). This is the series used for the surge signal.
- `upcoming_near_score`: the portion of scheduled upcoming activity starting within the next sample interval
- `upcoming_score`: end-state estimate over the full lookahead (`ongoing − expiring_full + starting_full`). Admin-dashboard only — it mostly reflects the expiry schedule of *current* alerts, so it is not sent to the LLM and drives no signal.
- `weights_version`: the weight-table version the score was computed under. Baselines and history only compare same-version rows — otherwise every weight calibration would poison the baseline with old-scale scores for up to the category's full window (4 weeks for roadworks). Bump `WEIGHTS_VERSION` in `pulse_categories.py` whenever weights change.

**Per-category time windows:**

| Category | Sample interval | History depth | Lookahead |
|----------|----------------|---------------|-----------|
| Transport | 1h | 24h (24 points) | 24h |
| Weather | 6h | 3 days (12 points) | 48h |
| Roadworks | Daily | 4 weeks (28 points) | 1 week |
| Events | Daily | 1 week (7 points) | 1 week |
| Incidents | Daily | 1 week (7 points) | None |

**Statistical baseline** — Computed from nonzero history scores when ≥3 data points exist, with two guards:

- **Lagged window**: the trailing 3 buckets are excluded (`BASELINE_LAG_BUCKETS`), so an ongoing episode can't absorb itself into the baseline within hours and normalize itself away (observed 2026-07-01: p75 jumped 44→131 in ~5h and status flapped severe↔moderate all night).
- **Version filter**: only same-`weights_version` rows enter the baseline (and the history shown to the LLM).

Fields: `baseline.mean` (typical level), `baseline.p25` (quieter than 75% of past periods), `baseline.p75` (busier than 75% of past periods).

**Deterministic status** — Derived from `ongoing_score` and `baseline`, then passed through two safeguards:

| Status | Condition |
|--------|-----------|
| clear | `ongoing_score == 0` |
| minor | `score ≤ min(mean, p75)` (or no baseline yet) |
| moderate | `min(mean, p75) < score ≤ max(mean, p75)` |
| severe | `score > max(mean, p75)` |

The `min`/`max` guard prevents an empty moderate band when skewed history puts the mean above p75.

- **Absolute floor** (`compute_status_floor`): where a source has an authoritative severity scale, it overrides the relative baseline — an ongoing DWD severity-3 warning floors weather at `moderate`, severity 4 at `severe`. Without this, an extreme storm after three calm days would read `minor` (no baseline → minor).
- **Hysteresis** (`apply_status_hysteresis`): escalations apply immediately; de-escalations only after the raw status has been lower for 2 consecutive hourly runs. State is kept in the `meta` table (`pulse_status_state`); re-runs within the same hour don't consume the confirmation. This stops boundary flapping. The pre-hysteresis value is logged as `raw_status`.
- **Skip guard** (`_should_skip_pulse`): the calm-interval skip (see [pulse_methodology.html](../web/templates/pulse_methodology.html)) is cancelled not only when a fast category (transport/weather) is currently moderate/severe, but also when its effective status just changed from the last *published* pulse — e.g. the hour hysteresis confirms a de-escalation. Otherwise the published pulse's narrative would keep describing a status (and severity) that no longer holds until the next scheduled interval elapses.

**Deterministic trend** (`compute_trend`) — current score vs. the mean of the 3 preceding buckets with a dead band of `max(15%, 1.0)`; above the band → `worsening`, below → `improving`, inside → `stable`.

**Surge signal** (`compute_surge`) — a fully deterministic escalation check. True when `scheduled_upcoming_score ≥ 1.5 × schedule_baseline.mean` AND at least half of that scheduled load starts within the next sample interval (with no baseline yet: imminent load ≥ 2.0). Judged from pure future starts only, never from expiry schedules of current alerts — an alert ending on its normal schedule (e.g. a nightly RMV replacement service finishing at 03:00) must never register as a surge on its own. A surge escalates the trend one step (improving→stable, stable→worsening) and is passed to the LLM as `surge_expected` for bridging narrative.

`schedule_baseline` (`_build_schedule_baseline`) is built from `scheduled_upcoming_score`'s own history — a separate series from the `ongoing_score` baseline used for status. High-baseload categories (transport, roadworks) can carry an ongoing baseline in the hundreds, driven by dozens of already-known chronic disruptions; a single new alert's schedule weight is only ever a few points, so it's compared against the *scheduled-load* baseline instead, which sits at a matching scale. This baseline is drawn from an independent, wider lookback window (`lookahead_hours + SCHEDULE_BASELINE_POOL_HOURS`, currently 168h) with a lag exclusion sized to the category's own `lookahead_hours` — wide enough that a disruption sitting visible for most of the lookahead window can't absorb itself into its own "typical" before it's evaluated. This window is independent of `history_hours` (the ongoing/status baseline's window), so it has no effect on status thresholds.

Status and trend are **not assigned by the LLM**. The LLM receives both as context.

### Layer 2 — LLM synthesis

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

Gemini Flash receives: the active alerts (with bodies), the timeseries data (including computed status, trend, surge flag, and baseline), and recent pulse/summary history. Its role is **narrative only** — it does not assign status or trend.

**Narrative output:**
- **Title** (≤40 chars) — glanceable headline
- **Summary** (≤300 chars) — cross-source synthesis, spatial awareness of Frankfurt districts, no enumeration of specifics users already see
- **Recommendation** (≤100 chars) — one actionable sentence, practical and calm
- **References** — up to 3 alert IDs most relevant to the summary
- **Trend override** (rare) — alert *content* sometimes carries direction the scores can't see ("service resumes at 14:30", "conditions will intensify overnight"). The LLM may correct a category's trend only in that case, and must supply the category, the corrected trend, and a reason quoting the alert content. Overrides are validated (known category, valid trend, non-empty reason) and logged as `trend_overrides_applied` in the debug record.

The LLM's value is interpretation: connecting a police incident to a transit disruption at the same location, recognizing that 6 S-Bahn delays converge on the same corridor, or noting that roadworks have been running for three weeks and are not newsworthy.

Thinking is enabled (`thinkingBudget: 1024`) for spatial reasoning.

### Layer 3 — Output assembly

`pulse.py` combines Layer 1 status and trend (plus any validated Layer 2 content override) to produce the final pulse:

```json
{
  "generated_at": "...",
  "title": "...",
  "summary": "...",
  "recommendation": "...",
  "categories": {
    "transport": {"status": "minor", "trend": "worsening"}
  },
  "alert_count": 42,
  "references": ["id1", "id2"]
}
```

**Temporal compression** — The system operates at three timescales:
- **Hourly pulse**: generated every hour, stored in `pulse_history`
- **Daily summary**: generated at 23:00 from the day's pulses, stored in `pulse_daily_summary`
- **History context**: each pulse receives the last 3 hourly pulses + last 3 daily summaries, enabling multi-day narrative references

## Debug log

Each pulse appends one JSON line to a daily JSONL file in `data/pulse_debug/` (retained 30 days). Structure:

```json
{
  "generated_at": "2026-06-30T10:00:00Z",
  "service": "gemini_pulse",
  "usage": {"tokens_in": 4200, "tokens_out": 310, "tokens_thinking": 0},
  "layer_1_deterministic": {
    "timeseries": {
      "transport": {
        "current": {
          "status": "minor",
          "raw_status": "minor",
          "trend": "stable",
          "surge_expected": false,
          "ongoing": {"count": 8, "score": 6.5},
          "projected": {"count": 6, "score": 5.0},
          "upcoming": {"total_score": 3.0, "near_score": 1.5},
          "horizon": {"total_score": 5.5, "near_score": 1.5}
        },
        "history": [
          {"hour": "2026-06-30T09:00:00Z", "count": 7, "score": 6.0, "scheduled_score": 2.5}
        ],
        "baseline": {"mean": 5.8, "p25": 3.2, "p75": 8.2, "n": 24},
        "window": "24h hourly"
      }
    },
    "score_breakdown": {
      "transport": {
        "ongoing": [
          {"alert_id": "HIM_123", "source": "rmv", "weight": 1.5, "title": "S1 delays", "body": "Signal failure near Frankfurt Hbf..."}
        ],
        "expiring_near": [],
        "starting_near": [],
        "starting_full": []
      }
    },
    "total_alerts": 42,
    "fresh_alerts": 15,
    "stale_summary": "12 autobahn, 8 baustellen"
  },
  "layer_2_llm": {
    "model": "gemini-2.5-flash",
    "prompt": "full prompt text sent to Gemini",
    "response": {
      "title": "S1 delays + A661 works",
      "summary": "S1 experiencing signal-related delays around Hbf...",
      "recommendation": "Allow extra time on S1; consider U-Bahn alternatives.",
      "references": ["HIM_123"],
      "trend_override": []
    }
  },
  "trend_overrides_applied": {},
  "layer_3_output": {
    "generated_at": "...",
    "title": "S1 delays + A661 works",
    "summary": "...",
    "categories": {"transport": {"status": "minor", "trend": "stable"}},
    "recommendation": "...",
    "alert_count": 42,
    "references": ["HIM_123"]
  }
}
```

**Reading the debug log:**
- **Layer 1**: Were scores correct? Check `score_breakdown` — which alerts contributed, with what weight, and into which bucket. Is `status` correct given `baseline`? `raw_status` shows the pre-hysteresis value; `trend` and `surge_expected` are deterministic.
- **Layer 2**: What did Gemini receive, and is the narrative faithful to it? The LLM outputs neither status nor trend — only narrative and, rarely, a `trend_override` with a quoted reason.
- **Layer 3**: Final output = Layer 1 status + trend, with any validated override applied (see `trend_overrides_applied`).

The admin dashboard (`/admin`) reads these files directly and visualizes all three layers with per-category score charts, breakdown tables, and history.

## Self-improving calibration loop

Status accuracy improves over time through a structured feedback loop.

**Step 1 — Admin override with reasoning**
When the computed status is wrong, the admin records a correction in the dashboard:
- Selects the correct status
- Provides a reason: e.g. "baustellen partial closures are routine — weight 1.0 too high"

Stored in `status_overrides` (pulse timestamp, category, computed status, override status, reason). Does not change live output — retrospective learning signal only.

**Step 2 — Weight review**
Admin-triggered from the dashboard. Sends recent overrides + score breakdowns + the current weight table to Gemini, which returns suggested weight adjustments with rationale. The admin reviews and applies manually to `pulse_categories.py`.

**The loop:**
```
Deterministic status → Wrong? Record override + reason
→ Weight review → Suggested adjustment → Apply to weights
→ Better scores → Better status → Fewer overrides
```

## Current limitations

- **No alert archive** — Only active alerts are retained; removed alerts are cleared. Pattern analysis ("S1 disrupted 3 times this week") is not possible.
- **Single geographic scope** — All of Frankfurt is treated as one zone.
- **Baseline requires history** — `baseline` is absent for new categories, after DB resets, and for a re-learn period after each `WEIGHTS_VERSION` bump; status defaults to `minor` for any non-zero score (except where an absolute floor applies, e.g. DWD severity).
- **Status is relative for most categories** — Outside weather's DWD floor, status measures "unusual vs. this category's own history", not absolute impact. A sustained multi-day disruption will still drift toward the baseline once it exceeds the lag window.

## Future improvements

- **Alert archive** — Persist alert lifecycle for historical pattern detection and recurrence analysis
- **Geographic clustering** — Detect when multiple alerts converge on the same area
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences
