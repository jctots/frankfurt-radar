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

This layer handles everything that can be computed precisely without interpretation:

**Ongoing-only filtering** — Only alerts with an active disruption window count toward trends. An alert announced today for a closure tomorrow does not inflate today's trend. The temporal filter:

- `valid_from <= now` AND (`valid_until >= now` OR absent) → counted (ongoing)
- `valid_from > now` → excluded (future, not yet a disruption)
- `valid_until < now` → excluded (expired)
- Sources without temporal data (polizei, strike — RSS feeds) → always counted

Future-only alerts still appear in the feed and in the LLM prompt for narrative context — they just don't move the EWMA.

**Severity weighting** — Each alert contributes a weight rather than a raw count of 1. Weights are deterministic, derived from existing alert fields:

| Source | Field | Weight mapping |
|--------|-------|----------------|
| DWD (weather) | `severity` (1–4) | minor=0.5, moderate=1.0, severe=1.5, extreme=2.0 |
| RMV (transport) | `service` | S-Bahn/U-Bahn/Regional=1.5, Tram/Bus=1.0 |
| Autobahn | `title_en` keyword | "closure"=1.5, else 1.0 |
| Baustellen | `service` | "City (Full)"=1.5, "City (Partial)"=1.0 |
| Events/Messe/Sports | — | Fixed 2.0 (one-off, high-impact) |
| Polizei/Strike | — | Default 1.0 (no structured severity data) |

The EWMA baseline and status/trend thresholds operate on these weighted scores, not raw alert counts.

**Status classification and trend detection** use Exponential Weighted Moving Average (EWMA) — a standard technique for event-count anomaly detection (used by the CDC for disease outbreak surveillance). EWMA gives more weight to recent data while older observations gradually fade, providing a self-correcting baseline that adapts to long-running conditions.

### EWMA-based status and trend

**Core formula:**
```
ewma = α × current_weighted_count + (1 - α) × previous_ewma
```

Where α (smoothing factor) controls responsiveness:
- α close to 1 → reacts fast, noisy
- α close to 0 → reacts slowly, smooth

**α = 0.3** for hourly data — smooth enough that long-running alerts (nearly half of all alerts run for >1 month) naturally become the baseline, while real changes surface within a few hours. EWMA is computed from the full 7-day pulse history (oldest-first traversal); the first count initializes the EWMA value.

**Storage:** EWMA value stored per category in pulse_history alongside the weighted count:
```json
{"transport": {"status": "low", "trend": "stable", "count": 9.5, "ewma": 6.2}}
```

`count` is the severity-weighted disruption score for ongoing alerts, not a raw alert count.

**Status classification** — Compare current weighted count against EWMA using sigma bands (σ = max(EWMA × 0.15, 1.0)):

| Status | Condition |
|--------|-----------|
| `clear` | Zero weighted count |
| `low` | Count ≤ EWMA + 1σ |
| `moderate` | Count > EWMA + 1σ |
| `high` | Count > EWMA + 2σ |

**Trend detection** — Measures the EWMA slope (is the baseline itself rising or falling?), decoupled from status. Compares the current EWMA against the previous EWMA value:

| Trend | Condition |
|-------|-----------|
| `stable` | EWMA change within ±5% |
| `worsening` | EWMA rose by > 5% |
| `improving` | EWMA fell by > 5% |

This decoupling allows all 12 status/trend combinations. For example, `high + improving` means "it's bad but getting better" (storm passing), while `low + worsening` means "it's fine but building up" (early warning).

**Cold start:** No history → EWMA is `None` → any non-zero count gets `moderate` status and `stable` trend. First pulse with data initializes the EWMA; subsequent pulses refine it. Trend requires at least 2 history pulses to compute a slope.

**Prior approaches:**
- **v0.9.7:** Used ratio-based thresholds (×1.3/×1.6 for status, ±30% for trend). Both status and trend compared count vs EWMA, making them coupled — only 4 of 12 combinations were possible. Production data showed everything stuck at `low + stable` 98% of the time.
- **v0.9.3:** Used a 7-day simple rolling average at the same hour (±1 hour window) for status, and compared status levels between consecutive pulses for trend. This had cross-hour comparison problems, required 7 days to warm up, and produced noisy trends.

### 2. LLM layer (Gemini Flash)

**Module:** `pulse.py` | **Prompt:** `prompts/pulse.md`

The LLM receives the active alerts, pre-computed categories (with a field reference explaining status/trend/count/ewma), and recent history as context. The prompt provides explicit guidance on how to use each input:

- **Categories**: feature only "moderate"/"high" status; reflect trend direction in language ("disruptions are increasing" for worsening, "easing" for improving)
- **History**: use for narrative continuity (avoid repeating summaries, reference multi-day patterns), not for trend/status judgments
- **Alerts**: synthesize across sources, don't enumerate — the feed already shows specifics

It produces two outputs:

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
    "alert_counts_by_category": {"weather": 1.5, "transport": 9.5, ...},
    "total_alerts": 42,
    "fresh_alerts": 15,
    "stale_summary": "12 autobahn, 8 baustellen",
    "ewma_per_category": {
      "transport": 6.2,
      ...
    },
    "computed_categories": {
      "transport": {"status": "moderate", "trend": "worsening", "count": 9.5, "ewma": 6.2},
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
- **Layer 1**: Were the weighted counts correct? What was the EWMA? Why did a category get `moderate` vs. `low`?
- **Layer 2**: What exact prompt did the LLM receive? Did it follow the tone and spatial awareness rules?
- **Layer 3**: Does the final output match what the deterministic layer computed?

Provide a debug log file together with this document as context when asking an LLM to suggest improvements.

## Current limitations

- **No alert archive** — Only active alerts are retained in `alert_cache`; removed alerts are cleared. Pattern analysis ("S1 disrupted 3 times this week") is not possible.
- **Single geographic scope** — All of Frankfurt is treated as one zone. A disruption in Sachsenhausen affects the same category as one in Bockenheim.

## Future improvements

- **Alert archive** — Persist every alert lifecycle (appeared, updated, removed) for historical pattern detection and recurrence analysis
- **Geographic clustering** — Detect when multiple alerts converge on the same area and flag spatial hotspots
- **Subscriber-personalized pulse** — Generate pulse variants filtered by subscriber preferences (e.g., only transit categories for a commuter)
- **Tunable α and thresholds** — Expose EWMA smoothing factor and status multipliers in config.yaml for per-deployment tuning
