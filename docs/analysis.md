# City Pulse — Analysis Approach

## Goal

City Pulse provides glanceable situational awareness for Frankfurt. It does not repeat alert titles — users already see those in the feed. Instead, it synthesizes alerts into actionable intelligence: what's the real impact, what correlates across sources, and what should someone do differently.

## Data sources

Frankfurt Radar collects real-time data from 8 pollers:

| Source | Category | What it provides |
|--------|----------|------------------|
| RMV (rmv) | Transport | Transit disruptions: S-Bahn, U-Bahn, tram, bus, regional |
| DWD (dwd) | Weather | Severe weather warnings with severity levels |
| Polizei (polizei) | Incidents | Police reports: accidents, closures, events |
| Autobahn (autobahn) | Roadworks | Federal road construction and closures |
| Baustellen (baustellen) | Roadworks | City road construction |
| Strike (strike) | Incidents | Strike alerts extracted from press releases |
| Events (events) | Events | City events: concerts, festivals, markets |
| Sports (sports) | Events | Match days, sports events |

Each poller runs on a cron schedule (typically every 5 minutes). Alerts are cached in `alert_cache` with timestamps, severity, geolocation, and staleness flags.

## Analysis pipeline

City Pulse processes data through three layers:

### 1. Deterministic layer (script)

**Module:** `pulse_categories.py`

This layer handles everything that can be computed precisely without interpretation:

**Alert counting** — Active, non-stale alerts are grouped by category using the source mapping above. Each category gets a current alert count.

**Status classification and trend detection** use Exponential Weighted Moving Average (EWMA) — a standard technique for event-count anomaly detection (used by the CDC for disease outbreak surveillance). EWMA gives more weight to recent data while older observations gradually fade, providing a self-correcting baseline that adapts to long-running conditions.

### Current implementation (v0.9.3)

Status uses a 7-day simple rolling average at the same hour (±1 hour window). Trend compares the current status level against the previous pulse's level. This approach has known issues:

- **Cross-hour comparison problem** — A `low` at 7am (baseline: 20 alerts) and a `high` at 9am (baseline: 0 alerts) are not comparable, leading to misleading trends.
- **7-day warm-up** — Cold start produces only `clear`/`low` until enough history accumulates.
- **1-hour trend window** — Comparing only to the previous pulse is too noisy and doesn't show trajectory.

### Planned: EWMA-based status and trend (v0.10)

Replace the simple rolling average with EWMA for both status and trend from a single mechanism.

**Core formula:**
```
ewma = α × current_count + (1 - α) × previous_ewma
```

Where α (smoothing factor) controls responsiveness:
- α close to 1 → reacts fast, noisy
- α close to 0 → reacts slowly, smooth

**Recommended α = 0.3** for hourly data — smooth enough that long-running alerts (nearly half of all alerts run for >1 month) naturally become the baseline, while real changes surface within a few hours.

**Storage:** EWMA value stored per category in pulse_history alongside count:
```json
{"transport": {"status": "low", "trend": "stable", "count": 8, "ewma": 6.2}}
```

**Status classification** — Compare current count against EWMA:

| Status | Condition |
|--------|-----------|
| `clear` | Zero alerts |
| `low` | Count ≤ EWMA × 1.3 |
| `moderate` | Count > EWMA × 1.3 |
| `high` | Count > EWMA × 1.6 |

**Trend detection** — Compare current count against EWMA (which encodes the trajectory of recent hours, not just the previous pulse):

| Trend | Condition |
|-------|-----------|
| `stable` | Count within ±30% of EWMA |
| `worsening` | Count > EWMA × 1.3 |
| `improving` | Count < EWMA × 0.7 |

**Advantages over current approach:**
- No cross-hour comparison problem — EWMA encodes the actual trajectory, not hour-specific snapshots
- No 7-day warm-up — works from the first pulse; first count = initial EWMA
- Long-running alerts naturally fade into the baseline via exponential decay
- Single parameter (α) to tune instead of multiple thresholds
- Status and trend derived from the same mechanism

### 2. LLM layer (Gemini Flash)

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

The LLM receives the active alerts, pre-computed categories, and recent history as context. It produces two outputs:

- **Summary** (≤300 chars) — Cross-source correlation, impact synthesis using a synthesis hierarchy (aggregate patterns, don't enumerate specifics), spatial awareness of Frankfurt districts
- **Recommendation** (≤100 chars) — One actionable sentence: practical and calm, no alarmist language

`travel_ok` is computed deterministically from category levels (false when transport or roadworks are `moderate` or `high`).

The LLM does **not** decide category statuses, trends, or travel_ok — those are computed by the deterministic layer. The LLM's value is in tasks that require interpretation:

- Connecting a police incident to a transit disruption at the same location
- Assessing that three separate S-Bahn delays converge on the same corridor
- Recommending tram 17 as an alternative when the U4 is suspended
- Recognizing that a construction alert has been running for weeks and is not newsworthy

Thinking is enabled (`thinkingBudget: 4096`) for spatial reasoning.

### 3. Temporal compression

The pipeline operates at three time scales:

**Hourly pulse** — Generated every hour from current active alerts. Stored in `pulse_history` with categories (including alert counts for baseline computation).

**Daily summary** — Generated at 23:00 by compressing 24 hourly pulses into a one-paragraph digest. Stored in `pulse_daily_summary`.

**History context** — Each hourly pulse receives the last 3 hourly pulses and last 3 daily summaries as context, enabling the LLM to write summaries that reference multi-day patterns ("roadworks on A5 entering their second week") without needing a full alert archive.

## Debug log

Each hourly pulse writes a structured JSON debug file to `data/pulse_debug/` (e.g., `2026-06-22T23.json`). Files are retained for 30 days.

The log structure mirrors the three analysis layers:

```json
{
  "generated_at": "2026-06-22T23:00:00Z",
  "current_hour_utc": 21,
  "layer_1_deterministic": {
    "alert_counts_by_category": {"weather": 1, "transport": 8, ...},
    "total_alerts": 42,
    "fresh_alerts": 15,
    "stale_summary": "12 autobahn, 8 baustellen",
    "baseline_7day": {
      "transport": {"avg": 6.5, "samples": 14},
      ...
    },
    "previous_pulse_categories": {
      "transport": {"status": "low", "count": 5},
      ...
    },
    "computed_categories": {
      "transport": {"status": "moderate", "trend": "worsening", "count": 8},
      ...
    }
  },
  "layer_2_llm": {
    "model": "gemini-2.5-flash",
    "prompt": "full prompt text sent to the LLM",
    "response": {"summary": "...", "travel_ok": true, "recommendation": "..."}
  },
  "layer_3_output": {
    "generated_at": "...",
    "summary": "...",
    "travel_ok": true,
    "categories": {...},
    "recommendation": "...",
    "alert_count": 42
  }
}
```

Use the debug log to review why a pulse produced a particular output:
- **Layer 1**: Were the alert counts correct? What was the baseline average? Why did a category get `moderate` vs. `low`?
- **Layer 2**: What exact prompt did the LLM receive? Did it follow the tone and spatial awareness rules?
- **Layer 3**: Does the final output match what the deterministic layer computed?

Provide a debug log file together with this document as context when asking an LLM to suggest improvements.

## Current limitations

- **Simple rolling average baseline (v0.9.3)** — Status uses a 7-day hour-matched average with known cross-hour comparison issues. EWMA replacement planned for v0.10.
- **No alert archive** — Only active alerts are retained in `alert_cache`; removed alerts are cleared. Pattern analysis ("S1 disrupted 3 times this week") is not possible.
- **No severity weighting** — All alerts count equally toward category status. A severe weather warning counts the same as a minor one.
- **Single geographic scope** — All of Frankfurt is treated as one zone. A disruption in Sachsenhausen affects the same category as one in Bockenheim.

## Future improvements

- **EWMA-based status and trend (v0.10)** — Replace simple rolling average with exponential weighted moving average for both status and trend from a single mechanism. See "Planned: EWMA-based status and trend" section above.
- **Alert archive** — Persist every alert lifecycle (appeared, updated, removed) for historical pattern detection and recurrence analysis
- **Severity-weighted status** — Weight alert counts by severity level so a single extreme weather warning outweighs three minor ones
- **Geographic clustering** — Detect when multiple alerts converge on the same area and flag spatial hotspots
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences (e.g., only transit categories for a commuter)
- **Tunable α and thresholds** — Expose EWMA smoothing factor and status multipliers in config.yaml for per-deployment tuning
