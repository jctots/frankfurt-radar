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
- `upcoming_count` / `upcoming_score`: number and severity-weighted score of alerts within the category's lookahead window

Snapshots are stored in the `category_snapshots` table (one row per category per hour).

**Per-category time windows** — Each category operates on its own natural timescale:

| Category | Sample interval | History depth | Upcoming lookahead |
|----------|----------------|---------------|--------------------|
| Transport | Hourly | 24h (24 points) | 6 hours |
| Weather | 6-hourly | 3 days (12 points) | 48 hours |
| Roadworks | Daily | 4 weeks (28 points) | 1 week |
| Events | Daily | 1 week (7 points) | 1 week |
| Incidents | Daily | 1 week (7 points) | None (retrospective) |

When building the LLM prompt context, hourly snapshot rows are aggregated to each category's sample interval (e.g. roadworks hourly rows → daily buckets using max count and score per bucket). History rows carry ongoing count and score only — upcoming values appear only in the current snapshot, since what's "upcoming" in one time slice becomes "ongoing" in the next.

### 2. LLM layer (Gemini Flash)

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

The LLM receives the active alerts, category time-series data, and recent history as context. It judges both the narrative summary and the per-category status and trend.

**Status judgment** — The LLM assigns each category a domain-specific status label:

| Category | Level 0 | Level 1 | Level 2 | Level 3 |
|----------|---------|---------|---------|---------|
| Transport | clear | delays | disrupted | paralyzed |
| Weather | clear | watch | warning | extreme |
| Roadworks | clear | works | closures | gridlock |
| Incidents | clear | low | elevated | major |
| Events | clear | crowds | busy | peak |

**Trend judgment** — The LLM compares current scores against the category's history and assigns: `improving`, `stable`, or `worsening`.

The LLM uses the time-series data (scores over time) combined with alert content to make these judgments. This approach gives the LLM the full context to judge severity — a single extreme-severity weather warning can warrant "warning" status even with a low alert count, and chronic low-level roadworks can be correctly identified as "works" rather than escalating due to count alone.

The LLM also produces:

- **Summary** (≤300 chars) — Cross-source correlation, impact synthesis using a synthesis hierarchy (aggregate patterns, don't enumerate specifics), spatial awareness of Frankfurt districts
- **Recommendation** (≤100 chars) — One actionable sentence: practical and calm, no alarmist language

The LLM's value is in tasks that require interpretation:

- Connecting a police incident to a transit disruption at the same location
- Assessing that three separate S-Bahn delays converge on the same corridor
- Recommending tram 17 as an alternative when the U4 is suspended
- Recognizing that a construction alert has been running for weeks and is not newsworthy
- Judging that 8 transport alerts with low individual severity collectively constitute "disrupted" status

Thinking is enabled (`thinkingBudget: 4096`) for spatial reasoning.

### 3. Temporal compression

The pipeline operates at three time scales:

**Hourly pulse** — Generated every hour from current active alerts. Stored in `pulse_history` with LLM-judged categories.

**Daily summary** — Generated at 23:00 by compressing 24 hourly pulses into a one-paragraph digest. Stored in `pulse_daily_summary`.

**History context** — Each hourly pulse receives the last 3 hourly pulses and last 3 daily summaries as context, enabling the LLM to write summaries that reference multi-day patterns ("roadworks on A5 entering their second week") without needing a full alert archive.

## Debug log

Each hourly pulse writes a structured JSON debug file to `data/pulse_debug/` (e.g., `2026-06-22T23.json`). Files are retained for 30 days.

The log structure mirrors the analysis layers:

```json
{
  "generated_at": "2026-06-22T23:00:00Z",
  "current_hour_utc": 21,
  "layer_1_deterministic": {
    "timeseries": {
      "transport": {
        "current": {"ongoing": {"count": 8, "score": 12.5}, "upcoming": {"count": 3, "score": 4.5}},
        "history": [{"hour": "2026-06-22T22:00:00Z", "count": 6, "score": 10.0}],
        "window": "24h hourly"
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
        "transport": {"status": "delays", "trend": "worsening"},
        "weather": {"status": "watch", "trend": "stable"}
      }
    }
  },
  "layer_3_output": {
    "generated_at": "...",
    "title": "...",
    "summary": "...",
    "categories": {"transport": {"status": "delays", "trend": "worsening"}},
    "recommendation": "...",
    "alert_count": 42,
    "references": ["id1", "id2"]
  }
}
```

Use the debug log to review why a pulse produced a particular output:
- **Layer 1**: Were the weighted scores correct? What does the time-series look like? Is the upcoming window capturing imminent alerts?
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
