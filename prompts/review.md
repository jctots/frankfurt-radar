---
model: gemini-2.5-pro
temperature: 0.3
max_output_tokens: 8192
thinking_budget: 4096
response_mime_type: application/json
---
You are reviewing the operation of City Pulse, an hourly AI-generated situational summary for Frankfurt (see docs/analysis.md for how it works). You are not looking at raw logs — a deterministic reducer has already turned {days} days of debug logs and database state into the digest below. Your job is judgment, not parsing.

## Digest

{digest_json}

## What each section means

- `prompt_template` / `prompt_samples`: the pulse prompt actually sent to the LLM this window — one deduped copy plus up to two additional rendered examples showing variation across hours. This prompt, and the methodology that produced its inputs, is the artifact under review.
- `pulse_hours`: one entry per generated pulse — `score_inputs` (the deterministic Layer 1 scores and top-weighted drivers), `llm_response` (what the pulse LLM wrote), `layer_3_output` (the final status/trend shown to users).
- `cost` / `translate`: spend and translation cache behaviour over the window.
- `overrides`: human corrections an admin recorded against a computed status — `[]` if none were recorded this window. Sharpen the severity-weight analysis when present; their absence does not block the review.
- `version_metrics`: per `pulse_config_version`, four log-derived metrics that are always available (status flap rate, trend-override rate, cost per pulse, coverage), plus `override_rate` only when overrides exist for that version.
- `db_crosschecks`: cost reconciliation (logged vs. recomputed from `api_usage`), pulse coverage (expected vs. produced hours, with gaps), and event-log anomalies (failures, restarts).

## Your task

Produce a structured report with these sections:

1. **Inconsistencies & bugs** — do the layers agree? Cost reconciliation deltas, coverage gaps, event-log errors, translation anomalies (unexpected retranslation, `text_changed` churn).
2. **Cost reduction** — where is spend concentrated (`cost.top_spenders`), and what levers reduce it without hurting quality?
3. **Severity weights** — are the weight mappings producing sensible scores given the observed alert mix in `pulse_hours`? If `overrides` is non-empty, let those corrections sharpen this section; if empty, reason from the score breakdowns and observed mix alone and say so plainly — do not treat the absence of overrides as a finding.
4. **Status & trend usefulness** — are computed `status`/`trend` values informative or noisy? Flapping, stuck-minor categories, baselines that never form.
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
