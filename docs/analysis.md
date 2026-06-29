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

City Pulse processes data through three layers:

### 1. Deterministic layer (script)

**Module:** `pulse_categories.py`

This layer computes severity-weighted scores and stores hourly snapshots. It handles everything that can be computed precisely without interpretation.

**Ongoing/upcoming filtering** — Alerts are classified into two groups:

- **Ongoing**: `valid_from <= now` AND (`valid_until >= now` OR absent) → active disruption
- **Upcoming**: `valid_from > now` AND within the category's lookahead window → imminent disruption
- **Excluded**: `valid_until < now` (expired) or `valid_from` beyond lookahead (too far out)
- Sources without temporal data (polizei, strike — RSS feeds) → always counted as ongoing

**Severity weighting** — Each alert contributes a weight rather than a raw count of 1. Weights are deterministic, derived from existing alert fields:

| Source | Field | Weight mapping |
|--------|-------|----------------|
| DWD (weather) | `severity` (1–4) | minor=0.5, moderate=1.0, severe=1.5, extreme=2.0 |
| RMV (transport) | `service` | S-Bahn/U-Bahn/Regional=1.5, Tram/Bus=1.0 |
| Autobahn | `title_en` keyword | "closure"=1.5, else 1.0 |
| Baustellen | `service` | "City (Full)"=1.5, "City (Partial)"=1.0 |
| Events/Messe/Sports | — | Fixed 2.0 (one-off, high-impact) |
| Polizei/Strike | — | Default 1.0 (no structured severity data) |

**Hourly snapshots** — Every hour, the pipeline stores a snapshot for each category containing:
- `ongoing_count` / `ongoing_score`: number and severity-weighted score of active alerts
- `projected_count` / `projected_score`: estimated ongoing score at the end of the **next sample interval** (1h for Transport, 6h for Weather, 24h for Roadworks/Events), computed as: `ongoing_score - expiring_near + starting_near`, where `expiring_near` and `starting_near` only count alerts within one sample interval, not the full lookahead window. This makes `projected_score` directly comparable to `ongoing_score` and to each history data point — they all cover the same time scale.
- `upcoming_count` / `upcoming_score`: number and severity-weighted score of all alerts starting within the category's full lookahead window. Used to compute the rate-of-growth signal (horizon momentum) by comparing across consecutive snapshots.
- `upcoming_near_score`: the portion of `upcoming_score` that falls within the next sample interval. Gives the LLM a proximity signal — a high ratio of `upcoming_near_score / upcoming_score` means the upcoming activity is imminent.

Snapshots are stored in the `category_snapshots` table (one row per category per hour).

**Per-category time windows** — Each category operates on its own natural timescale:

| Category | Sample interval | History depth | Upcoming lookahead |
|----------|----------------|---------------|--------------------|
| Transport | Hourly | 24h (24 points) | 6 hours |
| Weather | 6-hourly | 3 days (12 points) | 48 hours |
| Roadworks | Daily | 4 weeks (28 points) | 1 week |
| Events | Daily | 1 week (7 points) | 1 week |
| Incidents | Daily | 1 week (7 points) | None (retrospective) |

When building the LLM prompt context, hourly snapshot rows are aggregated to each category's sample interval (e.g. roadworks hourly rows → daily buckets using max count and score per bucket). Each history entry carries `count`, `score` (ongoing), and `horizon_score` (full-lookahead total at that point in time). The `horizon_score` series across history entries gives the LLM the rate-of-growth signal for horizon momentum — a rising sequence means new alerts are being published faster than old ones are dropping off.

### 2. LLM layer (Gemini Flash)

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

The LLM receives the active alerts, category time-series data, and recent history as context. It judges both the narrative summary and the per-category status and trend.

**Status judgment** — The LLM assigns each category a universal status label:

| Level | Label | Meaning |
|-------|-------|---------|
| 0 | clear | No ongoing alerts |
| 1 | minor | Score within typical baseline range |
| 2 | moderate | Score significantly above baseline, or high-impact content |
| 3 | severe | Score far above baseline and widespread impact confirmed |

**Trend judgment** — The LLM assigns trend (`improving`, `stable`, `worsening`) from two computed signals that feed a single consolidated label:

- **Signal 1 (next-interval projection)**: compares `ongoing_score` against `projected_score`, which covers only the next sample interval (1h/6h/24h). Combined with the history shape, this is the default basis for trend.
- **Signal 2 (horizon momentum)**: tracks how the full-lookahead `upcoming_score` changes across recent snapshots (`horizon.samples`). This may **override** the Signal 1 trend, but only when both conditions are met: (a) the rate of growth is **sharp** (clear acceleration, not noise), and (b) the newly-detected activity is **near** (high `horizon.near_score` relative to `horizon.total_score`). When both conditions hold, the LLM escalates trend and uses bridging language in the narrative to connect the near-term signal with the longer-horizon one. When only one condition holds, the horizon signal may appear in the narrative but does not change the trend label.

The LLM uses the time-series data (scores over time) combined with alert content to make these judgments. This approach gives the LLM the full context to judge severity — a single extreme-severity weather warning can warrant "warning" status even with a low alert count, and chronic low-level roadworks can be correctly identified as "works" rather than escalating due to count alone.

The LLM also produces:

- **Summary** (≤300 chars) — Cross-source correlation, impact synthesis using a synthesis hierarchy (aggregate patterns, don't enumerate specifics), spatial awareness of Frankfurt districts
- **Recommendation** (≤100 chars) — One actionable sentence: practical and calm, no alarmist language

The LLM's value is in tasks that require interpretation:

- Connecting a police incident to a transit disruption at the same location
- Assessing that three separate S-Bahn delays converge on the same corridor
- Recommending tram 17 as an alternative when the U4 is suspended
- Recognizing that a construction alert has been running for weeks and is not newsworthy
- Judging that 8 transport alerts with low individual severity collectively constitute "moderate" status

Thinking is enabled (`thinkingBudget: 1024`) for spatial reasoning.

### 3. Temporal compression

The pipeline operates at three time scales:

**Hourly pulse** — Generated every hour from current active alerts. Stored in `pulse_history` with LLM-judged categories.

**Daily summary** — Generated at 23:00 by compressing 24 hourly pulses into a one-paragraph digest. Stored in `pulse_daily_summary`.

**History context** — Each hourly pulse receives the last 3 hourly pulses and last 3 daily summaries as context, enabling the LLM to write summaries that reference multi-day patterns ("roadworks on A5 entering their second week") without needing a full alert archive.

## Debug log

Each hourly pulse appends a structured JSON line to a daily JSONL file in `data/pulse_debug/` (e.g., `2026-06-22.jsonl`). Files are retained for 30 days.

The log structure mirrors the analysis layers:

```json
{
  "generated_at": "2026-06-22T23:00:00Z",
  "current_hour_utc": 21,
  "layer_1_deterministic": {
    "timeseries": {
      "transport": {
        "current": {
          "ongoing": {"count": 8, "score": 12.5},
          "projected": {"count": 6, "score": 10.0},
          "horizon": {"total_score": 4.5, "near_score": 2.0}
        },
        "history": [{"hour": "2026-06-22T22:00:00Z", "count": 6, "score": 10.0, "horizon_score": 3.5}],
        "window": "24h hourly"
      }
    },
    "score_breakdown": {
      "transport": {
        "ongoing": [{"alert_id": "HIM_123", "source": "rmv", "weight": 1.5}],
        "expiring_near": [{"alert_id": "HIM_123", "source": "rmv", "weight": 1.5}],
        "starting_near": [],
        "starting_full": [{"alert_id": "HIM_789", "source": "rmv", "weight": 1.0}]
      }
    },
    "total_alerts": 42,
    "fresh_alerts": 15,
    "stale_summary": "12 autobahn, 8 baustellen"
  },
  "layer_2_llm": {
    "model": "gemini-2.5-flash",
    "prompt": "full prompt text sent to the LLM",
    "response": {
      "title": "...",
      "summary": "...",
      "recommendation": "...",
      "references": ["id1", "id2"],
      "categories": {
        "transport": {"status": "minor", "trend": "worsening"},
        "weather": {"status": "moderate", "trend": "stable"}
      }
    }
  },
  "layer_3_output": {
    "generated_at": "...",
    "title": "...",
    "summary": "...",
    "categories": {"transport": {"status": "minor", "trend": "worsening"}},
    "recommendation": "...",
    "alert_count": 42,
    "references": ["id1", "id2"]
  }
}
```

Use the debug log to review why a pulse produced a particular output:
- **Layer 1**: Were the weighted scores correct? Check `score_breakdown` to see which alerts landed in each bucket and with what weight. Is the projected score (next-interval) reflecting near-term direction? Is `horizon_score` in the history showing a rate-of-growth trend?
- **Layer 2**: What exact prompt did the LLM receive? Did it follow the tone and spatial awareness rules? Did it assign appropriate status labels given the data?
- **Layer 3**: Does the final output include valid category judgments?

Provide a debug log file together with this document as context when asking an LLM to suggest improvements.

## Current limitations

- **No alert archive** — Only active alerts are retained in `alert_cache`; removed alerts are cleared. Pattern analysis ("S1 disrupted 3 times this week") is not possible.
- **Single geographic scope** — All of Frankfurt is treated as one zone. A disruption in Sachsenhausen affects the same category as one in Bockenheim.

## Future improvements

- **Alert archive** — Persist every alert lifecycle (appeared, updated, removed) for historical pattern detection and recurrence analysis
- **Geographic clustering** — Detect when multiple alerts converge on the same area and flag spatial hotspots
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences (e.g., only transit categories for a commuter)
