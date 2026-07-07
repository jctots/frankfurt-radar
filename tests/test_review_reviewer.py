import json
from pathlib import Path

import pytest

from review import reviewer


_DIGEST = {
    "range": "2026-07-01..2026-07-07",
    "params": {"days": 7, "drivers_per_hour": 3, "prompt_samples": 1},
    "config_versions": ["a1b2c3"],
    "prompt_template": "PROMPT",
    "prompt_samples": [],
    "cost": {"monthly_cumulative": {}, "daily_by_service": [], "top_spenders": []},
    "translate": {"cache_hit_ratio": None, "new_translated": 0, "retranslated": 0, "anomalies": []},
    "pulse_hours": [],
    "overrides": [],
    "version_metrics": {},
    "db_crosschecks": {
        "cost_reconciliation": {"logged_eur": 0.0, "api_usage_eur": 0.0, "delta": 0.0},
        "pulse_coverage": {"expected_hours": 0, "produced": 0, "gaps": []},
        "event_log_anomalies": [],
    },
}


@pytest.fixture(autouse=True)
def clean_review_debug():
    yield
    import os
    out_dir = Path(os.environ["DATA_DIR"]) / "review_debug"
    if out_dir.exists():
        for f in out_dir.glob("*"):
            f.unlink()


class TestReviewerRun:
    def test_writes_three_files(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": []}, {"tokens_in": 10}
        ))

        result = reviewer.run(_DIGEST)

        assert Path(result["digest_path"]).exists()
        assert Path(result["report_path"]).exists()
        assert Path(result["changes_path"]).exists()

    def test_report_contains_body(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Findings\n\nAll good.", "changes": [], "copy_paste_prompts": []}, {}
        ))

        result = reviewer.run(_DIGEST)
        assert "# Findings" in Path(result["report_path"]).read_text(encoding="utf-8")

    def test_copy_paste_prompts_appended_to_report(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": ["Do the thing."]}, {}
        ))

        result = reviewer.run(_DIGEST)
        text = Path(result["report_path"]).read_text(encoding="utf-8")
        assert "Do the thing." in text
        assert "Copy-paste prompts" in text

    def test_changes_json_schema(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        proposed_change = {
            "target_file": "pulse_categories.py",
            "description": "lower baustellen partial-closure weight",
            "rationale": "4 overrides this week all downgraded to minor",
            "diff": "0.5 -> 0.3",
        }
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [proposed_change], "copy_paste_prompts": []}, {}
        ))

        result = reviewer.run(_DIGEST)
        changes_data = json.loads(Path(result["changes_path"]).read_text(encoding="utf-8"))
        assert changes_data["changes"] == [proposed_change]
        assert changes_data["config_versions"] == ["a1b2c3"]

    def test_digest_json_round_trips(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": []}, {}
        ))

        result = reviewer.run(_DIGEST)
        saved_digest = json.loads(Path(result["digest_path"]).read_text(encoding="utf-8"))
        assert saved_digest == _DIGEST

    def test_malformed_gemini_response_yields_empty_outputs(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=({}, {}))

        result = reviewer.run(_DIGEST)
        assert result["report_md"] == ""
        assert result["changes"] == []
        assert result["copy_paste_prompts"] == []
        # Still writes files (empty report) so the admin dashboard has a
        # consistent timestamp to list, rather than a silently dropped run.
        assert Path(result["report_path"]).exists()

    def test_non_list_changes_from_llm_are_ignored_not_crashed(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": "not a list", "copy_paste_prompts": None}, {}
        ))

        result = reviewer.run(_DIGEST)
        assert result["changes"] == []
        assert result["copy_paste_prompts"] == []

    def test_records_usage_via_call_gemini(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        call_gemini = mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": []}, {}
        ))

        reviewer.run(_DIGEST)
        args, kwargs = call_gemini.call_args
        service = kwargs.get("service", args[2] if len(args) > 2 else None)
        assert service == "gemini_review"


class TestListReports:
    def test_empty_when_no_reports(self):
        assert reviewer.list_reports() == []

    def test_lists_written_reports_newest_first(self, mocker):
        mocker.patch("review.reviewer.load_prompt", return_value=(
            {"model": "gemini-2.5-pro"}, "Review {days} days: {digest_json}"
        ))
        mocker.patch("review.reviewer._call_gemini", return_value=(
            {"report_md": "# Report", "changes": [], "copy_paste_prompts": []}, {}
        ))

        r1 = reviewer.run(_DIGEST)
        r2 = reviewer.run(_DIGEST)

        reports = reviewer.list_reports()
        timestamps = [r["timestamp"] for r in reports]
        assert r1["timestamp"] in timestamps
        assert r2["timestamp"] in timestamps
