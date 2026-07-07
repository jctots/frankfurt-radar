import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

import db
from review.reduce import estimate_cost, reduce


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _pulse_record(hour: str, version: str, status: str = "minor", weight: float = 1.5,
                   trend_override=None, prompt="PROMPT TEXT"):
    return {
        "generated_at": hour,
        "service": "gemini_pulse",
        "pulse_config_version": version,
        "usage": {"tokens_in": 1000, "tokens_out": 100, "tokens_thinking": 0},
        "layer_1_deterministic": {
            "timeseries": {
                cat: {"current": {"status": status, "trend": "stable", "ongoing": {"score": 5.0}}}
                for cat in ("weather", "transport", "roadworks", "incidents", "events")
            },
            "score_breakdown": {
                "transport": {"ongoing": [
                    {"alert_id": "HIM_1", "source": "rmv", "weight": weight, "title": "t", "body": "b"},
                ]},
            },
        },
        "layer_2_llm": {
            "model": "gemini-2.5-flash",
            "prompt": prompt,
            "response": {
                "title": "Title", "summary": "Summary", "recommendation": "Rec",
                "references": ["HIM_1"], "trend_override": trend_override or [],
            },
        },
        "trend_overrides_applied": {},
        "layer_3_output": {
            "generated_at": hour,
            "categories": {cat: {"status": status, "trend": "stable"} for cat in
                           ("weather", "transport", "roadworks", "incidents", "events")},
        },
    }


@pytest.fixture
def debug_dirs():
    data_dir = Path(os.environ["DATA_DIR"])
    dirs = [data_dir / "pulse_debug", data_dir / "cost_debug", data_dir / "translate_debug"]
    yield data_dir
    for d in dirs:
        for f in d.glob("*.jsonl"):
            f.unlink()


class TestReduceShape:
    def test_digest_has_expected_top_level_keys(self, debug_dirs, config):
        digest = reduce(days=1, now=datetime(2026, 7, 10, 12, tzinfo=timezone.utc), config=config)
        for key in ("range", "params", "config_versions", "weight_tables", "prompt_template",
                    "prompt_sample_texts", "cost", "translate", "pulse_hours", "status_distribution",
                    "overrides", "version_metrics", "db_crosschecks"):
            assert key in digest

    def test_empty_window_returns_empty_digest(self, debug_dirs, config):
        digest = reduce(days=1, now=datetime(2026, 7, 10, 12, tzinfo=timezone.utc), config=config)
        assert digest["pulse_hours"] == []
        assert digest["prompt_template"] is None
        assert digest["config_versions"] == []

    def test_gemini_extraction_and_daily_records_excluded(self, debug_dirs, config):
        # pulse_debug/*.jsonl is a shared log file — gemini_extraction and
        # gemini_daily records land there too, with a completely different
        # shape. They must not be treated as pulse hours.
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1"),
            {"generated_at": "2026-07-01T10:40:05Z", "service": "gemini_extraction",
             "extraction_type": "polizei", "usage": {}, "model": "gemini-2.5-flash"},
            {"generated_at": "2026-07-01T21:00:04Z", "service": "gemini_daily",
             "usage": {}, "date_summarized": "2026-07-01", "pulse_count": 14},
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert len(digest["pulse_hours"]) == 1
        assert digest["pulse_hours"][0]["hour"] == "2026-07-01T10:00:00Z"
        # baseline is legitimately None absent history — excluded from the check
        assert all(v is not None for k, v in digest["pulse_hours"][0]["score_inputs"]["transport"].items()
                   if v != [] and k != "baseline")


class TestWeightTables:
    def test_matches_live_pulse_categories_values(self, debug_dirs, config):
        import pulse_categories
        digest = reduce(days=1, now=datetime(2026, 7, 10, 12, tzinfo=timezone.utc), config=config)
        wt = digest["weight_tables"]
        assert wt["weights_version"] == pulse_categories.WEIGHTS_VERSION
        assert wt["dwd_severity"] == pulse_categories.SEVERITY_WEIGHTS_DWD
        assert wt["rmv_service"] == pulse_categories.SERVICE_WEIGHTS_RMV
        assert wt["baustellen_service"] == pulse_categories.SERVICE_WEIGHTS_BAUSTELLEN
        assert wt["events_weight"] == pulse_categories.WEIGHT_EVENTS
        assert wt["strike_weight"] == pulse_categories.WEIGHT_STRIKE
        assert wt["polizei_weight"] == pulse_categories.WEIGHT_POLIZEI
        assert wt["feuerwehr_weight"] == pulse_categories.WEIGHT_FEUERWEHR

    def test_present_even_with_no_pulse_hours(self, debug_dirs, config):
        # static reference data — not derived from the window's pulse_hours
        digest = reduce(days=1, now=datetime(2026, 7, 10, 12, tzinfo=timezone.utc), config=config)
        assert digest["pulse_hours"] == []
        assert digest["weight_tables"]["dwd_severity"]


class TestDriverContent:
    def test_top_driver_includes_title_source_body_not_just_id_weight(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl",
                     [_pulse_record("2026-07-01T10:00:00Z", "v1", weight=6.0)])
        digest = reduce(days=3, now=now, config=config)
        drivers = digest["pulse_hours"][0]["score_inputs"]["transport"]["top_drivers"]
        assert drivers == [{"alert_id": "HIM_1", "weight": 6.0, "source": "rmv", "title": "t", "body": "b"}]

    def test_drivers_per_hour_zero_still_returns_no_drivers(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl",
                     [_pulse_record("2026-07-01T10:00:00Z", "v1")])
        digest = reduce(days=3, drivers_per_hour=0, now=now, config=config)
        assert digest["pulse_hours"][0]["score_inputs"]["transport"]["top_drivers"] == []


class TestBaselinePassthrough:
    def test_baseline_included_when_present_in_timeseries(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        rec = _pulse_record("2026-07-01T10:00:00Z", "v1")
        rec["layer_1_deterministic"]["timeseries"]["transport"]["baseline"] = {
            "mean": 8.2, "p25": 4.0, "p75": 11.0, "n": 41,
        }
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", [rec])
        digest = reduce(days=3, now=now, config=config)
        assert digest["pulse_hours"][0]["score_inputs"]["transport"]["baseline"] == {
            "mean": 8.2, "p25": 4.0, "p75": 11.0, "n": 41,
        }

    def test_baseline_none_when_absent(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl",
                     [_pulse_record("2026-07-01T10:00:00Z", "v1")])
        digest = reduce(days=3, now=now, config=config)
        assert digest["pulse_hours"][0]["score_inputs"]["transport"]["baseline"] is None


class TestPromptDedup:
    def test_prompt_appears_once_as_template_plus_samples(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1", prompt="PROMPT A"),
            _pulse_record("2026-07-01T11:00:00Z", "v1", prompt="PROMPT B"),
            _pulse_record("2026-07-01T12:00:00Z", "v1", prompt="PROMPT C"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, drivers_per_hour=1, prompt_samples=1, now=now, config=config)
        assert digest["prompt_template"] == "PROMPT A"
        assert digest["prompt_sample_texts"] == ["PROMPT B"]
        # The full rendered prompt is not repeated per pulse_hour entry.
        for h in digest["pulse_hours"]:
            assert "prompt" not in h

    def test_prompt_sample_texts_empty_by_default(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [_pulse_record("2026-07-01T10:00:00Z", "v1", prompt="PROMPT A")]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["params"]["prompt_samples"] == 0
        assert digest["prompt_sample_texts"] == []
        assert digest["prompt_template"] == "PROMPT A"

    def test_prompt_sample_texts_capped_at_requested_count(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [_pulse_record(f"2026-07-01T{h:02d}:00:00Z", "v1", prompt=f"P{h}") for h in range(5)]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, prompt_samples=2, now=now, config=config)
        assert len(digest["prompt_sample_texts"]) == 2


def _translate_record(timestamp: str, entries: list[dict], total_alerts: int = 10, cached: int = 9):
    return {
        "timestamp": timestamp,
        "total_alerts": total_alerts,
        "cached": cached,
        "new_translated": 0,
        "retranslated": len(entries),
        "entries": entries,
    }


class TestTranslateAnomalyConcentration:
    def test_paid_churn_ranked_by_retranslate_count(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _translate_record("2026-07-01T00:00:00Z", [
                {"alert_id": "noisy-1", "action": "retranslate", "reason": "text_changed"},
                {"alert_id": "noisy-1", "action": "retranslate", "reason": "text_changed"},
                {"alert_id": "quiet-1", "action": "retranslate", "reason": "text_changed"},
            ]),
        ]
        _write_jsonl(debug_dirs / "translate_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        top = digest["translate"]["paid_churn"]["top_alerts"]
        assert top[0] == {"alert_id": "noisy-1", "count": 2, "share": pytest.approx(2 / 3, abs=1e-4)}
        assert digest["translate"]["paid_churn"]["total"] == 3

    def test_variant_hit_never_counted_as_paid_churn(self, debug_dirs, config):
        # A zero-cost, cache-served alert must never dominate the cost signal
        # — this is the exact bug that made a real 1121-hit alert (all
        # variant_hit, zero retranslate) look like a cost driver.
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        entries = [{"alert_id": "free-flapper", "action": "variant_hit", "reason": "text_changed"} for _ in range(50)]
        _write_jsonl(debug_dirs / "translate_debug" / "2026-07-01.jsonl", [_translate_record("2026-07-01T00:00:00Z", entries)])

        digest = reduce(days=3, now=now, config=config)
        assert digest["translate"]["paid_churn"]["total"] == 0
        assert digest["translate"]["paid_churn"]["top_alerts"] == []
        assert digest["translate"]["cache_churn"]["total"] == 50
        assert digest["translate"]["cache_churn"]["top_alerts"][0]["alert_id"] == "free-flapper"

    def test_samples_capped_and_deduped_by_alert_id(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        entries = [{"alert_id": "same-id", "action": "retranslate", "reason": "text_changed"} for _ in range(50)]
        _write_jsonl(debug_dirs / "translate_debug" / "2026-07-01.jsonl", [_translate_record("2026-07-01T00:00:00Z", entries)])

        digest = reduce(days=3, now=now, config=config)
        # 50 raw entries for one alert_id collapse to a single sample, not 50.
        assert digest["translate"]["paid_churn"]["samples"] == [
            {"alert_id": "same-id", "action": "retranslate", "reason": "text_changed"}
        ]
        assert digest["translate"]["paid_churn"]["total"] == 50

    def test_no_anomalies_returns_empty_buckets(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        assert digest["translate"]["paid_churn"] == {"total": 0, "top_alerts": [], "samples": []}
        assert digest["translate"]["cache_churn"] == {"total": 0, "top_alerts": [], "samples": []}


class TestStatusDistribution:
    def test_counts_status_per_category(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1", status="minor"),
            _pulse_record("2026-07-01T11:00:00Z", "v1", status="minor"),
            _pulse_record("2026-07-01T12:00:00Z", "v1", status="severe"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["status_distribution"]["transport"] == {"minor": 2, "severe": 1}

    def test_empty_window_has_empty_distributions_for_every_category(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        for cat in ("weather", "transport", "roadworks", "incidents", "events"):
            assert digest["status_distribution"][cat] == {}

    def test_distribution_reflects_full_window_not_just_one_version(self, debug_dirs, config):
        # status_distribution is a window-wide tally, unlike version_metrics
        # which only groups tagged (post-deploy) hours.
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1", status="minor"),
            {**_pulse_record("2026-07-01T11:00:00Z", "v1", status="moderate"), "pulse_config_version": None},
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["status_distribution"]["transport"] == {"minor": 1, "moderate": 1}


class TestVersionGrouping:
    def test_groups_hours_by_config_version(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1"),
            _pulse_record("2026-07-01T11:00:00Z", "v1"),
            _pulse_record("2026-07-01T12:00:00Z", "v2"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["config_versions"] == ["v1", "v2"]
        assert set(digest["version_metrics"].keys()) == {"v1", "v2"}


class TestVersionMetrics:
    def test_status_flap_rate_detects_transitions(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1", status="minor"),
            _pulse_record("2026-07-01T11:00:00Z", "v1", status="moderate"),
            _pulse_record("2026-07-01T12:00:00Z", "v1", status="minor"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["version_metrics"]["v1"]["status_flap_rate"] > 0

    def test_trend_override_rate(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T10:00:00Z", "v1"),
            _pulse_record("2026-07-01T11:00:00Z", "v1",
                           trend_override=[{"category": "transport", "trend": "improving", "reason": "x"}]),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert digest["version_metrics"]["v1"]["trend_override_rate"] == 0.5

    def test_cost_per_pulse_eur_computed_from_hourly_usage(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [_pulse_record("2026-07-01T10:00:00Z", "v1")]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        with db._conn() as conn:
            conn.execute(
                """INSERT INTO api_usage_hourly (hour, service, calls, tokens_in, tokens_out, tokens_thinking, characters)
                   VALUES ('2026-07-01T10', 'gemini_pulse', 1, 1000000, 0, 0, 0)"""
            )
            # A different service's usage in the same hour must not leak in.
            conn.execute(
                """INSERT INTO api_usage_hourly (hour, service, calls, tokens_in, tokens_out, tokens_thinking, characters)
                   VALUES ('2026-07-01T10', 'gemini_review', 1, 5000000, 0, 0, 0)"""
            )

        digest = reduce(days=3, now=now, config=config)
        # 1M input tokens * $0.15/M * 0.92 USD->EUR default = ~€0.138, / 1 pulse.
        assert digest["version_metrics"]["v1"]["cost_per_pulse_eur"] == pytest.approx(0.138, abs=1e-4)

    def test_coverage_present_for_each_version(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [_pulse_record("2026-07-01T10:00:00Z", "v1")]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert 0.0 <= digest["version_metrics"]["v1"]["coverage"] <= 1.0

    def test_override_rate_only_present_when_overrides_exist(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        recs = [_pulse_record("2026-07-01T10:00:00Z", "v1")]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=3, now=now, config=config)
        assert "override_rate" not in digest["version_metrics"]["v1"]

        db.add_status_override("2026-07-01T10:00:00Z", "transport", "minor", "moderate", "looked worse")
        digest = reduce(days=3, now=now, config=config)
        assert digest["version_metrics"]["v1"]["override_rate"] == 1.0


class TestCostReconciliation:
    def test_reconciliation_present(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        rec = digest["db_crosschecks"]["cost_reconciliation"]
        assert set(rec.keys()) == {"logged_eur", "api_usage_eur", "delta"}

    def test_delta_zero_with_no_usage(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        assert digest["db_crosschecks"]["cost_reconciliation"]["delta"] == 0.0

    def test_month_boundary_does_not_double_count_prior_month(self, debug_dirs, config):
        # Window spans June 30 -> July 3. A full June's worth of api_usage
        # must not be added on top of July's — both logged_eur and
        # api_usage_eur are "cost so far in July", the same accounting basis.
        db.record_api_usage("gemini_pulse", tokens_in=1_000_000, tokens_out=0, tokens_thinking=0)
        with db._conn() as conn:
            conn.execute("UPDATE api_usage SET month = '2026-06' WHERE service = 'gemini_pulse'")
            conn.execute("UPDATE api_usage_hourly SET hour = '2026-06-30T10' WHERE service = 'gemini_pulse'")

        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        rec = digest["db_crosschecks"]["cost_reconciliation"]
        # No July usage recorded and no cost_debug snapshots -> both sides
        # must reflect July only (0), not June's cost bleeding in.
        assert rec["api_usage_eur"] == 0.0
        assert rec["delta"] == 0.0


class TestCoverageGaps:
    def test_missing_hour_reported_as_gap(self, debug_dirs, config):
        now = datetime(2026, 7, 1, 2, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T00:00:00Z", "v1"),
            # 01:00 missing — a gap
            _pulse_record("2026-07-01T02:00:00Z", "v1"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=1, now=now, config=config)
        coverage = digest["db_crosschecks"]["pulse_coverage"]
        assert "2026-07-01T01:00Z" in coverage["gaps"]
        assert coverage["produced"] == coverage["expected_hours"] - len(coverage["gaps"])

    def test_pulse_history_row_without_debug_record_not_a_gap(self, debug_dirs, config):
        # pulse_history is authoritative — a real generation the debug log
        # never recorded (a truncated write) must not read as a missing hour.
        now = datetime(2026, 7, 1, 2, tzinfo=timezone.utc)
        db.store_pulse({"generated_at": "2026-07-01T01:00:00Z", "summary": "s"})
        recs = [
            _pulse_record("2026-07-01T00:00:00Z", "v1"),
            _pulse_record("2026-07-01T02:00:00Z", "v1"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=1, now=now, config=config)
        coverage = digest["db_crosschecks"]["pulse_coverage"]
        assert "2026-07-01T01:00Z" not in coverage["gaps"]
        assert coverage["debug_log_truncated"] == ["2026-07-01T01:00Z"]

    def test_debug_only_hour_not_flagged_as_truncated(self, debug_dirs, config):
        # An expected interval skip is in pulse_debug but never written to
        # pulse_history — that's normal, not a truncation signal.
        now = datetime(2026, 7, 1, 2, tzinfo=timezone.utc)
        recs = [
            _pulse_record("2026-07-01T00:00:00Z", "v1"),
            {"generated_at": "2026-07-01T01:00:02Z", "service": "gemini_pulse", "skipped": True,
             "reason": "all calm", "layer_1_deterministic": {"timeseries": {}}},
            _pulse_record("2026-07-01T02:00:00Z", "v1"),
        ]
        _write_jsonl(debug_dirs / "pulse_debug" / "2026-07-01.jsonl", recs)

        digest = reduce(days=1, now=now, config=config)
        coverage = digest["db_crosschecks"]["pulse_coverage"]
        assert coverage["debug_log_truncated"] == []
        assert "2026-07-01T01:00Z" not in coverage["gaps"]


class TestRedaction:
    def test_subscribers_never_leak_into_digest(self, debug_dirs, config):
        with db._conn() as conn:
            conn.execute(
                "INSERT INTO subscribers (chat_id, preferences) VALUES (?, ?)",
                (999999999, "{}"),
            )
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        serialized = json.dumps(digest)
        assert "999999999" not in serialized
        assert "chat_id" not in serialized


class TestEstimateCost:
    def test_returns_tokens_and_eur(self, debug_dirs, config):
        now = datetime(2026, 7, 3, 12, tzinfo=timezone.utc)
        digest = reduce(days=3, now=now, config=config)
        est = estimate_cost(digest)
        assert est["tokens"] >= 0
        assert est["eur"] >= 0.0


class TestRealDebugLogsSmoke:
    def test_high_detail_token_estimate_under_budget(self, debug_dirs, config, monkeypatch):
        repo_root = Path(__file__).parent.parent
        if not (repo_root / "pulse_debug").exists():
            pytest.skip("real 7-day debug logs not present in this checkout")
        monkeypatch.setenv("DATA_DIR", str(repo_root))
        # High detail preset is drivers_per_hour=5, not "all" (web/templates/admin.html) —
        # driver entries now carry title+body, so unbounded "all" on a busy category
        # (seen up to ~76 alerts/hour in production) would blow well past budget.
        digest = reduce(days=7, drivers_per_hour=5, prompt_samples=2, config=config)
        est = estimate_cost(digest)
        # docs/review.md indicative table: high detail, 7 days ~= 200K tokens.
        assert est["tokens"] < 500_000

    def test_unbounded_drivers_is_deliberately_expensive(self, debug_dirs, config, monkeypatch):
        """drivers_per_hour=None ("all") is a manual, not preset, choice — it is
        not expected to stay under the same budget as the high-detail preset."""
        repo_root = Path(__file__).parent.parent
        if not (repo_root / "pulse_debug").exists():
            pytest.skip("real 7-day debug logs not present in this checkout")
        monkeypatch.setenv("DATA_DIR", str(repo_root))
        digest = reduce(days=7, drivers_per_hour=None, prompt_samples=2, config=config)
        est = estimate_cost(digest)
        assert est["tokens"] > 500_000
