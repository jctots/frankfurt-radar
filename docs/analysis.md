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

**Status classification** — The current count is compared against a 7-day rolling average at the same hour of day (±1 hour window). This produces a unified status level:

| Status | Meaning |
|--------|---------|
| `clear` | Zero alerts in this category |
| `low` | At or below the historical average — normal for this time of day |
| `moderate` | Above the historical average — noticeable increase |
| `high` | Significantly above average (>1.6×) — unusual situation |

The baseline is **self-correcting**: if roadworks are consistently high, that becomes the new normal. A weekday rush-hour baseline differs from a Sunday morning baseline because comparisons are hour-matched.

**Cold start:** When there is no historical data (first pulse, or first pulse at this hour), alerts present default to "low" and zero alerts default to "clear". The baseline becomes accurate after approximately 7 days of data.

**Trend detection** — The current status level is compared against the previous pulse's level:

| Comparison | Trend |
|------------|-------|
| Same level | `stable` |
| Higher level | `worsening` |
| Lower level | `improving` |
| No previous pulse | `stable` |

### 2. LLM layer (Gemini Flash)

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

The LLM receives the active alerts, pre-computed categories, and recent history as context. It produces three outputs:

- **Summary** (≤200 chars) — Cross-source correlation, impact synthesis, not repetition
- **Recommendation** (≤100 chars) — One actionable sentence: name the alternative route, suggest an event
- **travel_ok** (bool) — Whether transit/roads have significant active disruptions

The LLM does **not** decide category statuses or trends — those are computed by the deterministic layer. The LLM's value is in tasks that require interpretation:

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

- **No alert archive** — Only active alerts are retained in `alert_cache`; removed alerts are cleared. Pattern analysis ("S1 disrupted 3 times this week") is not possible.
- **Baseline warm-up** — The adaptive baseline needs ~7 days of data at each hour to be meaningful. During this period, status classification uses cold-start defaults.
- **No severity weighting** — All alerts count equally toward category status. A severe weather warning counts the same as a minor one.
- **Single geographic scope** — All of Frankfurt is treated as one zone. A disruption in Sachsenhausen affects the same category as one in Bockenheim.

## Future improvements

- **Alert archive** — Persist every alert lifecycle (appeared, updated, removed) for historical pattern detection and recurrence analysis
- **Severity-weighted status** — Weight alert counts by severity level so a single extreme weather warning outweighs three minor ones
- **Geographic clustering** — Detect when multiple alerts converge on the same area and flag spatial hotspots
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences (e.g., only transit categories for a commuter)
- **Confidence scoring** — Track how often the LLM's travel_ok assessment matches the deterministic category levels to calibrate trust
- **Tunable thresholds** — Expose the status classification multipliers (1.1×, 1.6×) in config.yaml for per-deployment tuning
