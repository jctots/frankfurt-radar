# City Pulse — Analysis Approach

## Goal

City Pulse provides glanceable situational awareness for Frankfurt. It does not repeat alert titles — users already see those in the feed. Instead, it synthesizes alerts into actionable intelligence: what's the real impact, what correlates across sources, and what should someone do differently.

## Data sources

Frankfurt Radar collects real-time data from 9 pollers:

| Source | Category | What it provides |
|--------|----------|------------------|
| RMV (rmv) | Transport | Transit disruptions: S-Bahn, U-Bahn, tram, bus, regional |
| DWD (dwd) | Weather | Severe weather warnings with severity levels |
| Polizei (polizei) | Incidents | Police reports: accidents, closures, events |
| Autobahn (autobahn) | Roadworks | Federal road construction and closures |
| Baustellen (baustellen) | Roadworks | City road construction |
| Strike (strike) | Incidents | Strike alerts extracted from press releases |
| Events (events) | Events | City festivals: concerts, parades, markets |
| Messe (messe) | Events | Trade fairs at Messe Frankfurt |
| Sports (sports) | Events | Match days, sports events |

Each poller runs on a cron schedule (typically every 5 minutes). Alerts are cached in `alert_cache` with timestamps, severity, geolocation, and staleness flags.

## Analysis pipeline

City Pulse processes data through three layers on each hourly run.

### Layer 1 — Deterministic scoring

**Module:** `pulse_categories.py`

Computes severity-weighted scores, stores hourly snapshots, and derives the status label for each category. Everything here is deterministic — no LLM involved.

**Alert classification** — Alerts are placed into buckets:

- **Ongoing**: `valid_from ≤ now` AND (`valid_until ≥ now` OR absent) → active disruption
- **Upcoming**: `valid_from > now` AND within the category's lookahead window → imminent
- **Excluded**: expired alerts or alerts beyond the lookahead window
- **No-temporal sources** (polizei, strike — RSS feeds): always counted as ongoing

**Severity weighting** — Each alert contributes a weight derived from its fields:

| Source | Field | Weight mapping |
|--------|-------|----------------|
| DWD | `severity` (1–4) | minor=0.5, moderate=1.0, severe=1.5, extreme=2.0 |
| RMV | `service` | S-Bahn/U-Bahn/Regional=1.5, Tram/Bus=1.0 |
| Autobahn | `title_en` keyword | "closure"=1.5, else 1.0 |
| Baustellen | `service` | "City (Full)"=1.5, else 1.0 |
| Events/Messe/Sports | — | Fixed 2.0 |
| Polizei/Strike | — | Default 1.0 |

**Hourly snapshots** — Stored in `category_snapshots` (one row per category per hour):

- `ongoing_score`: severity-weighted sum of active alerts
- `projected_score`: estimated score at the end of the next sample interval — `ongoing_score − expiring_near + starting_near`. Directly comparable to `ongoing_score`.
- `upcoming_score`: severity-weighted sum of all alerts starting within the full lookahead window
- `upcoming_near_score`: portion of `upcoming_score` falling within the next sample interval

**Per-category time windows:**

| Category | Sample interval | History depth | Lookahead |
|----------|----------------|---------------|-----------|
| Transport | 1h | 24h (24 points) | 6h |
| Weather | 6h | 3 days (12 points) | 48h |
| Roadworks | Daily | 4 weeks (28 points) | 1 week |
| Events | Daily | 1 week (7 points) | 1 week |
| Incidents | Daily | 1 week (7 points) | None |

**Statistical baseline** — Computed from the history window when ≥3 data points exist:
- `baseline.mean`: average ongoing score — the "typical" level for this category
- `baseline.p75`: 75th percentile — above this is genuinely elevated

**Deterministic status** — Derived from `ongoing_score` and `baseline`:

| Status | Condition |
|--------|-----------|
| clear | `ongoing_score == 0` |
| minor | `score ≤ baseline.mean` (or no baseline yet) |
| moderate | `baseline.mean < score ≤ baseline.p75` |
| severe | `score > baseline.p75` |

Status is **not assigned by the LLM**. The LLM receives the computed status as context.

### Layer 2 — LLM synthesis

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

Gemini Flash receives: the active alerts (with bodies), the timeseries data (including computed status and baseline), and recent pulse/summary history. Its role is **trend and narrative only** — it does not assign or override status.

**Trend judgment** — Per category: `improving` / `stable` / `worsening`. Two signals:

- **Signal 1 (next-interval projection)**: compares `ongoing_score` vs `projected_score` combined with history shape. This is the default basis.
- **Signal 2 (horizon momentum)**: overrides Signal 1 only when both conditions hold — the `horizon_score` series shows sharp acceleration (not just drift) AND the activity is near (`near_score` is a high fraction of `total_score`). When triggered, the LLM uses bridging language in the narrative.

**Narrative output:**
- **Title** (≤40 chars) — glanceable headline
- **Summary** (≤300 chars) — cross-source synthesis, spatial awareness of Frankfurt districts, no enumeration of specifics users already see
- **Recommendation** (≤100 chars) — one actionable sentence, practical and calm
- **References** — up to 3 alert IDs most relevant to the summary

The LLM's value is interpretation: connecting a police incident to a transit disruption at the same location, recognizing that 6 S-Bahn delays converge on the same corridor, or noting that roadworks have been running for three weeks and are not newsworthy.

Thinking is enabled (`thinkingBudget: 1024`) for spatial reasoning.

### Layer 3 — Output assembly

`pulse.py` combines Layer 1 status with Layer 2 trend to produce the final pulse:

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
          "ongoing": {"count": 8, "score": 6.5},
          "projected": {"count": 6, "score": 5.0},
          "horizon": {"total_score": 3.0, "near_score": 1.5}
        },
        "history": [
          {"hour": "2026-06-30T09:00:00Z", "count": 7, "score": 6.0, "horizon_score": 2.5}
        ],
        "baseline": {"mean": 5.8, "p75": 8.2, "n": 24},
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
      "categories": {
        "transport": {"trend": "stable"},
        "weather":   {"trend": "stable"},
        "roadworks": {"trend": "stable"},
        "incidents": {"trend": "stable"},
        "events":    {"trend": "stable"}
      }
    }
  },
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
- **Layer 1**: Were scores correct? Check `score_breakdown` — which alerts contributed, with what weight, and into which bucket. Is `status` correct given `baseline`?
- **Layer 2**: What did Gemini receive? Did it assign plausible trends? Note the LLM does not output `status` — only `trend` per category.
- **Layer 3**: Final output merges Layer 1 status + Layer 2 trend.

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
- **Baseline requires history** — `baseline` is absent for new categories or after DB resets; status defaults to `minor` for any non-zero score.

## Future improvements

- **Alert archive** — Persist alert lifecycle for historical pattern detection and recurrence analysis
- **Geographic clustering** — Detect when multiple alerts converge on the same area
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences
