# City Pulse — Review Pipeline

> Companion to [analysis.md](analysis.md). Where `analysis.md` describes how City Pulse *produces* a pulse, this document describes how an AI agent *reviews* the pipeline's own output — its logs, costs, and methodology — and proposes improvements.

## Goal

City Pulse runs unattended every hour and writes rich debug logs, but nobody reads 2.5 MB of JSONL a day. The review pipeline closes that gap: an admin-triggered AI agent that periodically audits the last N days of operation and returns a concrete, actionable report — inconsistencies and bugs, cost-reduction levers, severity-weight tuning, and whether `status`/`trend` are actually useful — plus proposed edits to the methodology itself (`analysis.md`, `pulse_methodology.html`, `prompts/pulse.md`, and the weight tables).

**Weight tuning is one section of this unified review**, alongside cost, translation, prompt quality, deterministic scoring, and cross-version comparison. When the admin has recorded *overrides* — marking "this status was wrong, here's why" into `status_overrides` — those corrections sharpen the weight and version analysis (see [Judging strategy versions](#judging-strategy-versions)). Overrides are an **optional enhancing signal, not a precondition**: they are exceptions the admin records only when a status looks wrong, and the review runs fully without any. Absent overrides, weight tuning reasons from the score breakdowns and observed alert mix alone, and the override-derived metric is simply omitted; every other section is unaffected.

**Design principle** — deterministic work is a script, judgment is the AI. A Python **reducer** does all parsing, aggregation, metric computation, and cross-checking (free, repeatable); the **LLM reviewer** only reasons over the reduced result. This is what keeps the operation affordable enough to run on demand.

## Why a two-stage design

The raw logs are hostile to direct LLM consumption. Each `pulse_debug` record is ~138 KB, of which the embedded prompt (~98 KB) and the raw per-alert score breakdown (~38 KB) are near-identical across the 27 records in a day. Seven days fed raw is ~15 MB (~4M tokens) — dominated by repeated boilerplate, not signal. Feeding that directly would cost several euros per run *to re-read the same prompt 189 times*.

The reducer collapses that redundancy before any tokens are spent:

```
raw logs + radar.db  ──►  reduce.py (deterministic)  ──►  digest  ──►  Gemini reviewer  ──►  report
```

The digest is the **only** thing the reviewer sees. Its size — controlled by three integer knobs and previewed as an estimated cost before you spend — is the direct cost dial.

**Reduction happens at read time, not at generation.** The debug logs stay rich and un-reduced because they are a source of record with more than one consumer — the admin dashboard reads them directly to visualize all three layers, and future reviews may need detail this review doesn't. Stripping at write time would be lossy and irreversible, would kill the `drivers_per_hour` fidelity knob, and would couple the hourly production path to the reviewer's evolving needs. The redundancy that motivates reduction (the ~98 KB prompt repeated each hour) only ever costs *tokens*, which the reducer already removes; on disk it is ~75 MB over the 30-day retention — negligible. If write-side savings were ever needed, the answer would be lossless dedup (store the prompt once per `pulse_config_version` plus the hourly interpolated variables), never dropping data.

## Stage 1 — Deterministic reducer

**Module:** `review/reduce.py` — pure Python, deterministic, no network, no LLM.

**Reads** (last `days`, default 7):
- `data/cost_debug/*.jsonl` — hourly cost rollups + `monthly_cumulative` per service
- `data/translate_debug/*.jsonl` — per-run translation counts and `entries[]`
- `data/pulse_debug/*.jsonl` — the three-layer pulse records (now carrying `pulse_config_version`)
- `radar.db` — authoritative state/history (read-only), for cross-checks and human-correction signal

**Emits** one digest file to the data volume, `data/review_debug/<timestamp>.digest.json` (same data-folder pattern as `pulse_debug`/`cost_debug` — outputs live on the runtime volume, never in the repo):

```json
{
  "range": "2026-07-01..2026-07-07",
  "params": { "days": 7, "drivers_per_hour": 3, "prompt_samples": 1 },
  "config_versions": ["a1b2c3"],
  "prompt_template": "<the pulse prompt, extracted once — the artifact under review>",
  "prompt_samples": ["<N fully rendered prompts, N = prompt_samples>"],
  "cost": {
    "monthly_cumulative": { "total_eur": 1.53, "services": { "...": {"calls": 0, "cost_eur": 0.0} } },
    "daily_by_service": [ { "date": "...", "google_translate": 0.12, "gemini_pulse": 0.09 } ],
    "top_spenders": [ {"service": "google_translate", "eur": 0.85, "share": 0.56} ]
  },
  "translate": {
    "cache_hit_ratio": 0.98, "new_translated": 0, "retranslated": 3,
    "paid_churn": { "total": 3, "top_alerts": [ {"alert_id": "HIM_FREETEXT_2162899", "count": 3, "share": 1.0} ],
                    "samples": [ {"alert_id": "...", "action": "retranslate", "reason": "text_changed"} ] },
    "cache_churn": { "total": 1121, "top_alerts": [ {"alert_id": "baustellen-B-2025-00744", "count": 1121, "share": 1.0} ],
                      "samples": [ {"alert_id": "...", "action": "variant_hit", "reason": "text_changed"} ] }
  },
  "pulse_hours": [
    {
      "hour": "2026-07-06T10:00Z", "config_version": "a1b2c3",
      "score_inputs": { "transport": {"status": "minor", "trend": "stable",
                        "ongoing_score": 6.5, "top_drivers": ["HIM_123 w1.5", "..."]} },
      "llm_response": { "title": "...", "summary": "...", "recommendation": "...",
                        "references": ["..."], "trend_override": [] },
      "layer_3_output": { "categories": {"...": {"status": "...", "trend": "..."}} }
    }
  ],
  "overrides": [ {"hour": "...", "category": "baustellen", "computed": "moderate",
                  "corrected": "minor", "reason": "partial closures are routine"} ],
  "version_metrics": {
    "a1b2c3": { "override_rate": 0.04, "status_flap_rate": 0.10,
                "trend_override_rate": 0.02, "cost_per_pulse_eur": 0.009, "coverage": 0.99 }
  },
  "db_crosschecks": {
    "cost_reconciliation": { "logged_eur": 1.53, "api_usage_eur": 1.53, "delta": 0.0 },
    "pulse_coverage": { "expected_hours": 168, "produced": 167, "gaps": ["2026-07-03T04:00Z"],
                        "debug_log_truncated": [] },
    "event_log_anomalies": [ {"ts": "...", "level": "error", "msg": "..."} ]
  }
}
```

### Cost knobs — three integers, plus a pre-spend estimate

Cost is `days × 27 hours/day × detail-per-hour`, so the reducer exposes the actual drivers as integers rather than a coarse mode:

| Param | Type | Default | Effect on cost |
|---|---|---|---|
| `days` | int | **7** | linear |
| `drivers_per_hour` | int — top-N scoring alerts kept per category per hour; **0 = counts only** | **3** | linear |
| `prompt_samples` | int, 0–2 — fully rendered prompt examples | **1** | fixed step (~98 KB each) |

Two one-click presets sit on top of these for convenience: **high detail** = `(drivers=all, samples=2)`, **low detail** = `(0, 0)`. But the numbers are the real control.

**Cost preview.** Because the reducer runs *before* any tokens are spent, the admin page shows the built digest's **estimated tokens and EUR**, and you confirm before the Gemini call fires. You see the price, then decide to spend. Indicative figures (Gemini; verify against current pricing before wiring cost tracking):

| Digest | ~Tokens | Gemini 2.5 Pro / run | Gemini Flash / run |
|---|---|---|---|
| high detail, 7 days | ~200K | ~€0.30 | ~€0.03 |
| low detail, 7 days | ~40K | ~€0.06 | ~€0.01 |

A €1 monthly budget comfortably covers on-demand review — even a high-detail Pro run is ~€0.35.

### radar.db cross-checks (four tables, each earning its place)

Debug files are an *event stream* and can be truncated when a run crashes; `radar.db` is the authoritative record. The reducer reads four tables only:

- `status_overrides` → the **human corrections** that sharpen weight tuning and supply the override-rate metric when present (optional — the review runs without them).
- `api_usage` / `api_usage_hourly` → **reconcile logged cost against recorded cost**; catches miscounting. Both figures are "cost so far in the calendar month of the last `cost_debug` snapshot" — the same accounting basis, not the digest's day window (which would double-count whenever the window spans a month boundary).
- `pulse_history` → confirm every hour actually produced a pulse; surface silent gaps the debug logs don't show. A pulse hour counts as covered if *either* `pulse_history` (a real generation) or `pulse_debug` (a generation or an expected interval skip, which `pulse_history` never records) confirms it. `debug_log_truncated` isolates the specific failure the design above calls out: `pulse_history` confirms a generation but its debug record is missing entirely.
- `event_log` → errors and anomalies the happy-path debug records omit.

The other 13 tables stay out until a review concretely needs one.

### Redaction

The `subscribers` table is never opened. No subscriber-derived data (chat IDs, preferences, individual counts) enters the digest. GDPR scope is preserved by construction, not by discipline.

## Configuration versioning

To let the reviewer *attribute* output-quality changes to methodology changes rather than guess, every pulse run is stamped with a `pulse_config_version` — a short hash of the reviewable methodology as one unit:

```
pulse_config_version = hash(prompts/pulse.md  +  WEIGHTS_VERSION  +  window/strategy config)
```

Written into each `pulse_debug` record and carried into the digest. The reducer groups hours by version and computes per-version metrics; when a digest spans a version bump, the reviewer compares outcomes. Accepted recommendations *become* the changelog from one version to the next, closing the loop:

```
Review finds issue → propose prompt/weight/strategy edit → apply → config version bumps
→ next review measures whether the new version improved the finding
```

This reuses the existing `WEIGHTS_VERSION` mechanism (see [analysis.md](analysis.md)) and extends it to the prompt and strategy so the whole methodology is versioned together.

### Judging strategy versions

Pulse output has no ground truth, so the reviewer is **not** allowed to simply declare "v2 is better." "Better" is defined by version-tagged metrics the reducer computes deterministically. Four of them come straight from the logs and are always available; the fifth, the override rate, is the strongest single signal because it is human judgment, but it appears only when the admin has recorded corrections:

| Metric (per version, per category) | Proxy for | Better = | Always available? |
|---|---|---|---|
| status flap rate — status changes per hour | noise | lower | yes |
| trend-override rate — LLM correcting deterministic trend | deterministic-layer blind spots | lower | yes |
| cost per pulse | efficiency | lower | yes |
| coverage — fraction of hours that produced a pulse | reliability | higher | yes |
| **override rate** — admin corrections per pulse | calibration accuracy (human judgment) | lower | only when overrides exist |

The reviewer reports these numbers and the qualitative read behind them; it never asserts a winner without the metrics. This is, in effect, an A/B eval keyed on `pulse_config_version`. The four log-derived metrics carry the comparison on their own; the override rate, when present, is the strongest tie-breaker.

**Who reviews the review?** The metrics do, retrospectively — you are the backstop. If review *N* recommended "lower roadworks weight" and review *N+1* shows the roadworks metrics improved (fewer flaps, or a lower override rate if any were recorded) after you applied it, the recommendation was correct — measured, not asserted. Each report records its input digest hash and the config versions it covered, so any run is reproducible, and "trend across reports" surfaces recommendations that were made but never moved the metric. There is no infinite regress: the metrics close the loop, and you approve or reject every proposal.

## Stage 2 — LLM reviewer

**Module:** `review/reviewer.py` | **Prompt:** `prompts/review.md` | **Model:** Gemini, declared in the `prompts/review.md` frontmatter (same `load_prompt` mechanism as the pulse prompt; defaults to `gemini-2.5-pro` for the heavier reasoning). One provider, one key, unified cost accounting — changing the model is a prompt-file edit, not a config change.

Reads the digest only. Produces a structured report:

| Section | What it answers |
|---|---|
| **Inconsistencies & bugs** | Do the layers agree? Cost reconciliation deltas, pulse coverage gaps, event-log errors, `translate.cache_churn` concentration (source-data instability the cache is absorbing for free). |
| **Cost reduction** | Where is spend concentrated (`cost.top_spenders`, `translate.paid_churn.top_alerts`), and what levers reduce it without hurting quality — translator backend, dedup, prompt-length trade-offs. `cache_churn` is never a cost lever — it costs nothing by construction. |
| **Severity weights** | Are the `analysis.md` weight mappings producing sensible scores given the observed alert mix? Any recorded `overrides` sharpen this — but with none, the analysis proceeds from the score breakdowns alone. Concrete adjustments with rationale. |
| **Status & trend usefulness** | Are computed `status`/`trend` values informative or noisy? Flapping is one failure mode; never reaching `clear` is *not* one on its own (background-activity categories may legitimately never hit a literal zero score) — the real check is whether status ever escalates during genuinely elevated periods. Also: baselines that never form. |
| **Prompt quality** | Two directions, explicitly traded off: (a) *enhance* — richer instructions, higher quality, higher token cost; (b) *reduce* — shorter prompt, lower cost, some quality loss. Each recommendation names its direction and expected cost delta. |
| **Cross-version comparison** | When the digest spans versions, which version won on the [metrics above](#judging-strategy-versions) and why. |

Thinking is enabled for the reasoning-heavy weight-tuning and comparison sections.

### Report format — proposals delivered as a pull request

The reviewer **proposes; nothing lands until you merge.** A pull request is the delivery mechanism: it is a reviewable diff that changes the running system only when a human merges it — the concrete change is written out for inspection, and merging is the human-applied step.

Each run produces two things on the data volume:

- `<timestamp>.report.md` — the human-readable findings and rationale (rendered in-page, retained on the data volume).
- `<timestamp>.changes.json` — a machine-readable set of proposed edits (target file, old→new content or unified diff, and the linked finding/rationale) for concrete, mechanical changes: weight numbers, table rows, threshold constants.

CI turns `<timestamp>.changes.json` into a **draft PR** (see below). Example finding → change:

> **Finding:** roadworks over-scored — 4 baustellen "partial closure" overrides this week, all downgraded to `minor`.
> **Proposed edit** (`pulse_categories.py`): lower baustellen "City (Partial)" weight `0.5 → 0.3`, bump `WEIGHTS_VERSION`, add rationale row to `analysis.md`'s severity table.

**Copy-paste fallback for judgment-heavy changes.** Some findings are too open-ended to express as a diff — "restructure the prompt's spatial-reasoning section," "reconsider the transport lookahead window." Those stay in `report.md` as a ready-to-run implementation prompt you paste into your dev environment (Claude Code). Mechanical → PR; judgment → prompt.

### Opening the PR — CI-mediated, no credential on prod

The review runs server-side, but the files it changes live in the repo, so the GitHub write credential is deliberately kept **off** the public-facing prod server. The flow:

```
admin button → reduce() + reviewer (prod, Gemini key only)
→ writes report.md + changes.json to data/review_debug/
→ GitHub Actions workflow reads changes.json → creates branch → opens DRAFT PR
```

The GitHub Actions workflow already has repo write access by design; the prod web app never holds a GitHub token. The PR is always opened as a **draft**, references the source `report.md`, and is never auto-merged — you review, test, and merge (or close) it like any other PR. This also gives every proposed methodology change a normal review trail and CI run.

## Admin trigger

Manual only — **no cron**. The admin dashboard (`/admin`) has a **Run review** control:

- a `days` number input, prefilled `7`
- `drivers_per_hour` and `prompt_samples` inputs (with high/low-detail presets)
- a **Preview cost** step → shows estimated tokens + EUR from the built digest
- a **Run** button → fires the Gemini call

On run: `reduce(params)` builds the digest → `reviewer.run(digest)` calls Gemini → the report renders in-page and is written to `data/review_debug/<timestamp>.report.md` (with `.digest.json` and `.changes.json` beside it), same data-volume pattern as `pulse_debug`. Past reports are listed for re-reading. The run's own token cost is recorded in `api_usage` under a `gemini_review` service line, so review spend is visible alongside pulse spend. A separate GitHub Actions workflow picks up `changes.json` and opens the draft PR (see [Opening the PR](#opening-the-pr--ci-mediated-no-credential-on-prod)).

## Development methodology — gold standard and blind comparison

The reviewer prompt (`prompts/review.md`) is not written blind. It is validated against a **gold-standard reference**: a human-guided analysis of the same digest, recorded first. The reviewer then runs on the identical digest *without* seeing the reference, and the two are compared. Divergence shows where the prompt under-performs and drives its next revision.

Because both the reference analysis and the agent consume the **same Stage-1 digest**, the comparison isolates reasoning quality — not who parsed the files better. This is an eval harness for the review prompt, with the digest as the fixed input and the gold-standard findings as the rubric.

## What lives where

Code lives in the repo; all generated artifacts live on the runtime **data volume**, never in the repo — same split as the existing `*_debug` logs.

```
# in the repo
review/
├── reduce.py          # Stage 1 — deterministic reducer + metrics
└── reviewer.py        # Stage 2 — Gemini reviewer
prompts/
├── pulse.md           # the pulse prompt (reviewed)
└── review.md          # the reviewer prompt
docs/
├── analysis.md        # how the pulse is produced (reviewed)
└── review.md          # this document
.github/workflows/
└── review-pr.yml      # picks up changes.json, opens the draft PR

# on the data volume (not the repo)
data/review_debug/
├── <timestamp>.digest.json    # reduced input the reviewer saw
├── <timestamp>.report.md      # human-readable findings
└── <timestamp>.changes.json   # machine-readable proposed edits → PR
```

## Limitations

- **Point-in-time, not continuous** — review runs on demand over a fixed window; regressions between runs are only caught at the next run.
- **Reviewer reasons over a digest, not raw logs** — anything the reducer drops (e.g. per-alert bodies at low `drivers_per_hour`) is invisible to the reviewer. Raise the knobs, at higher cost, to mitigate.
- **Cross-version comparison needs a version bump in-window** — until a `pulse_config_version` change falls inside the reviewed days, there is nothing to compare and the report covers a single version.
- **Metrics are proxies** — the override rate needs the admin to have recorded corrections; with none in the window, that single metric is omitted and the four log-derived metrics carry the comparison (the review is not blocked).
- **Proposals only** — the reviewer never edits the running system; it opens a draft PR (or emits a copy-paste prompt) that changes nothing until you review, test, and merge it.

## Future improvements

- **Scheduled review** — promote the manual button to a weekly Gitea/GitHub Action once the reviewer prompt has stabilized against the gold standard.
- **Trend tracking across reports** — persist findings so recurring issues ("google_translate still 55% of spend") surface as "unresolved since 3 reviews ago."
- **Richer auto-diffs** — extend `changes.json` coverage to more change types (currently mechanical edits only; judgment-heavy rethinks remain copy-paste prompts).

## Feedback

Have a suggestion for improving the review methodology? [Open an issue on GitHub](https://github.com/jctots/frankfurt-radar/issues/new?template=feature_request.md).
