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
- `translate`: translation cache behaviour. `total_anomalies` is the total count of cache-miss/retranslation events; `top_churn_alerts` is the same data pre-aggregated by `alert_id` (count + share of `total_anomalies`) — use these counts, not `anomaly_samples`, to state how concentrated the churn is. `anomaly_samples` is deduplicated to one example per `alert_id`, already capped — it is illustrative only, its length says nothing about frequency.
- `overrides`: human corrections an admin recorded against a computed status — `[]` if none were recorded this window. Sharpen the severity-weight analysis when present; their absence does not block the review.
- `version_metrics`: per `pulse_config_version`, four log-derived metrics that are always available (status flap rate, trend-override rate, cost per pulse, coverage), plus `override_rate` only when overrides exist for that version.
- `db_crosschecks`: cost reconciliation (logged vs. recomputed from `api_usage`), pulse coverage (expected vs. produced hours, with gaps), and event-log anomalies (failures, restarts).

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

1. **Inconsistencies & bugs** — do the layers agree? Cost reconciliation deltas, coverage gaps, event-log errors, translation anomalies (unexpected retranslation, `text_changed` churn).
2. **Cost reduction** — where is spend concentrated (`cost.top_spenders`), and what levers reduce it without hurting quality?
3. **Severity weights** — are the weight mappings producing sensible scores given the observed alert mix in `pulse_hours`? If `overrides` is non-empty, let those corrections sharpen this section; if empty, reason from the score breakdowns and observed mix alone and say so plainly — do not treat the absence of overrides as a finding.
4. **Status & trend usefulness** — are computed `status`/`trend` values informative or noisy? Flapping is one failure mode (too much movement); the opposite is a category that never reaches `clear` across the whole window (too little movement — a permanent floor, not a signal). Check the status *distribution* per category in `pulse_hours`, not just the flap rate, and call out both kinds. Also: baselines that never form.
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
