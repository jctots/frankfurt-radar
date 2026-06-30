---
model: gemini-2.5-flash
temperature: 0.2
response_mime_type: application/json
---
You are reviewing the severity weight calibration for the Frankfurt Radar City Pulse system.

## Background

City Pulse computes a per-category severity-weighted score from active alerts. The score determines the status label (clear / minor / moderate / severe) using historical baselines (mean and p75). The scoring weights are defined in `pulse_categories.py`.

## Current weight table

{weight_table}

## Historical baselines (mean / p75 per category from recent data)

{baselines}

## Admin overrides (corrections with reasoning)

The following status overrides were recorded by the admin. Each entry means: "The computed status was wrong — the correct status should have been X, for this reason."

{overrides}

## Score breakdown samples

The following shows which alerts contributed to the score in recently overridden pulses:

{score_breakdowns}

## Your task

Analyze the overrides and their reasoning. Identify patterns — e.g. a specific source or service class is consistently over- or under-weighted relative to admin judgment.

Return a JSON object with this structure:

{{
  "analysis": "2-3 sentence summary of the main patterns observed",
  "suggestions": [
    {{
      "target": "source or service class to adjust (e.g. 'baustellen partial', 'rmv bus')",
      "current_weight": 1.0,
      "suggested_weight": 0.5,
      "rationale": "Admin consistently overrode moderate→minor when only baustellen partial closures were present. These are routine maintenance and should not push scores above the mean."
    }}
  ],
  "no_change": ["list of weight classes that appear correctly calibrated"]
}}

If there are not enough overrides to draw conclusions, return `"suggestions": []` and explain in `"analysis"`.
