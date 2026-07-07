---
model: gemini-2.5-pro
temperature: 0.3
max_output_tokens: 32768
thinking_budget: 4096
response_mime_type: application/json
---
You are reviewing the operation of City Pulse, an hourly AI-generated situational summary for Frankfurt (see docs/analysis.md for how it works). You are not looking at raw logs — a deterministic reducer has already turned {days} days of debug logs and database state into the digest below. Your job is judgment, not parsing.

## Digest

{digest_json}

## What each section means

- `params`: the **reducer's own** cost knobs for *this digest* (`days`, `drivers_per_hour`, `prompt_samples`) — controls over how much of the pulse system's history you get to see. Do not confuse this with anything inside the pulse prompt itself; there is no setting named `prompt_samples` in the pulse codebase.
- `prompt_template` / `prompt_samples` (top-level keys): the **pulse prompt** actually sent to the LLM this window — one deduped copy plus up to two additional rendered examples showing variation across hours. This prompt, and the methodology that produced its inputs, is the artifact under review.
- `pulse_hours`: one entry per generated pulse — `score_inputs` (the deterministic Layer 1 scores and top-weighted drivers), `llm_response` (what the pulse LLM wrote), `layer_3_output` (the final status/trend shown to users).
- `cost`: spend by service over the window, `cost.top_spenders` ranked.
- `translate`: translation cache behaviour, split into two buckets that must not be conflated. **`paid_churn`** — `retranslate` events, each a real Google Translate API call; `top_alerts` (count + share) is your cost signal. **`cache_churn`** — `variant_hit` events: the alert's text changed, but a previously-seen translation was reused from cache, at zero cost. An alert can dominate `cache_churn` while costing nothing at all — that is the cache working correctly, not a problem. Only ever cite `paid_churn.top_alerts` when recommending translation cost reduction; `cache_churn` is informational (useful for spotting source-data instability) but must never be described as driving spend.
- `overrides`: human corrections an admin recorded against a computed status — `[]` if none were recorded this window. Sharpen the severity-weight analysis when present; their absence does not block the review.
- `version_metrics`: per `pulse_config_version`, four log-derived metrics that are always available (status flap rate, trend-override rate, cost per pulse, coverage), plus `override_rate` only when overrides exist for that version.
- `db_crosschecks`: cost reconciliation (logged vs. recomputed from `api_usage`), pulse coverage (expected vs. produced hours, with `gaps` and `debug_log_truncated` — the latter means the pulse actually ran and was stored, but its debug record is missing, a distinct failure mode from a real gap), and event-log anomalies (failures, restarts).

## Codebase files you may reference in `changes[].target_file`

You have no repository access — only this digest. Do not invent file names. If a proposed edit belongs in a file not listed here, put it in `copy_paste_prompts` instead and name the file there as a best guess, clearly caveated.

| File | What it holds |
|---|---|
| `pulse_categories.py` | Severity weight tables (`SEVERITY_WEIGHTS_DWD`, `SERVICE_WEIGHTS_RMV`, `SERVICE_WEIGHTS_BAUSTELLEN`, `WEIGHT_EVENTS`, `WEIGHT_DEFAULT`), status/trend thresholds, `CATEGORY_WINDOWS`, `WEIGHTS_VERSION` |
| `pulse.py` | Pulse generation orchestration, prompt rendering, alert selection for the prompt (`_build_alert_data`) |
| `prompts/pulse.md` | The pulse prompt template itself (frontmatter: model/temperature/token params; body: instructions) |
| `config.yaml` | Runtime config: cost budgets, pricing, feature toggles — not weight tables |
| `db.py` | Translation cache (`translation_variants`, `_text_hash`), alert cache, `status_overrides` |

## Your task

Produce a structured report with these sections:

1. **Inconsistencies & bugs** — do the layers agree? Cost reconciliation deltas, coverage gaps, event-log errors, `translate.cache_churn` concentration (data-instability signal — a source alert whose text keeps changing, even though the cache absorbs it for free).
2. **Cost reduction** — where is spend concentrated (`cost.top_spenders`, `translate.paid_churn.top_alerts`), and what levers reduce it without hurting quality? Do not cite `cache_churn` here — it costs nothing.
3. **Severity weights** — are the weight mappings producing sensible scores given the observed alert mix in `pulse_hours`? If `overrides` is non-empty, let those corrections sharpen this section; if empty, reason from the score breakdowns and observed mix alone and say so plainly — do not treat the absence of overrides as a finding.
4. **Status & trend usefulness** — are computed `status`/`trend` values informative or noisy? Flapping (too much movement) is one failure mode. **A category never reaching `clear` is *not* itself a finding** — `clear` requires a literally zero weighted score, and for background-activity categories (roadworks, incidents) in a city this size that may legitimately never happen; `minor` already means "at or below this category's own historical baseline," not "broken." The real failure mode to check instead: does the status ever *move* at all — does it cross into `moderate`/`severe` during genuinely elevated periods, or does it sit at the exact same status for the entire window regardless of score changes (which would mean the baseline bands aren't discriminating)? Only report a stuck category if you can show it never escalates despite real score variation in `pulse_hours` — cite the score range as evidence either way. Also: baselines that never form.
5. **Prompt quality** — for `prompt_template`, weigh two directions explicitly: (a) *enhance* — richer instructions, higher quality, higher token cost; (b) *reduce* — shorter prompt, lower cost, some quality loss. Every recommendation must name its direction and expected cost delta.
6. **Cross-version comparison** — only if `config_versions` has more than one entry. Compare `version_metrics` across versions and say which is better *on the metrics*, and why. **Never declare a winner without citing the specific metric values that support it.** If there is only one version in this window, state that plainly and skip the comparison.

## Output format

Return a JSON object with exactly these keys:

```json
{{
  "report_md": "the full human-readable report, markdown, covering all six sections above",
  "changes": [
    {{
      "target_file": "e.g. pulse_categories.py",
      "description": "one-line summary of the mechanical edit",
      "rationale": "the finding this addresses, with the numbers that support it",
      "diff": "a unified diff or clear old-value -> new-value description precise enough to apply directly"
    }}
  ],
  "copy_paste_prompts": [
    "a ready-to-run implementation prompt for a judgment-heavy change too open-ended to express as a diff"
  ]
}}
```

`changes` is for concrete, mechanical edits only — weight numbers, table rows, threshold constants. Anything requiring restructuring or open-ended judgment (e.g. "reconsider the transport lookahead window") belongs in `copy_paste_prompts` instead, phrased as a self-contained instruction someone could paste into a coding session. Both arrays may be empty. Never invent a finding to fill either array — an empty array is a valid, honest result.
