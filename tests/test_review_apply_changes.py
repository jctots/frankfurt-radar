import subprocess

import pytest

from review.apply_changes import apply_changes, is_unified_diff, render_manual_changes_md


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    target = repo / "weights.py"
    target.write_text("WEIGHT = 0.5\n", encoding="utf-8")
    subprocess.run(["git", "add", "weights.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


_VALID_DIFF = """--- a/weights.py
+++ b/weights.py
@@ -1 +1 @@
-WEIGHT = 0.5
+WEIGHT = 0.3
"""


class TestIsUnifiedDiff:
    def test_detects_standard_unified_diff(self):
        assert is_unified_diff(_VALID_DIFF)

    def test_detects_git_diff_header(self):
        assert is_unified_diff("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")

    def test_rejects_freeform_description(self):
        assert not is_unified_diff("0.5 -> 0.3")

    def test_rejects_empty(self):
        assert not is_unified_diff("")
        assert not is_unified_diff(None)


class TestApplyChanges:
    def test_valid_diff_applied_to_working_tree(self, git_repo):
        change = {"target_file": "weights.py", "diff": _VALID_DIFF}
        applied, manual = apply_changes([change], git_repo)

        assert applied == [change]
        assert manual == []
        assert (git_repo / "weights.py").read_text(encoding="utf-8") == "WEIGHT = 0.3\n"

    def test_freeform_diff_falls_back_to_manual(self, git_repo):
        change = {"target_file": "weights.py", "diff": "0.5 -> 0.3", "description": "lower weight"}
        applied, manual = apply_changes([change], git_repo)

        assert applied == []
        assert manual == [change]
        # File untouched — nothing was silently applied.
        assert (git_repo / "weights.py").read_text(encoding="utf-8") == "WEIGHT = 0.5\n"

    def test_diff_that_fails_to_apply_falls_back_to_manual(self, git_repo):
        bad_diff = """--- a/weights.py
+++ b/weights.py
@@ -1 +1 @@
-WEIGHT = 999
+WEIGHT = 0.3
"""
        change = {"target_file": "weights.py", "diff": bad_diff}
        applied, manual = apply_changes([change], git_repo)

        assert applied == []
        assert manual == [change]

    def test_no_patch_file_left_behind(self, git_repo):
        change = {"target_file": "weights.py", "diff": _VALID_DIFF}
        apply_changes([change], git_repo)
        assert not (git_repo / ".review_patch.diff").exists()

    def test_mixed_changes_split_correctly(self, git_repo):
        good = {"target_file": "weights.py", "diff": _VALID_DIFF}
        bad = {"target_file": "other.py", "diff": "please restructure this"}
        applied, manual = apply_changes([good, bad], git_repo)
        assert applied == [good]
        assert manual == [bad]


class TestRenderManualChangesMd:
    def test_empty_list_returns_empty_string(self):
        assert render_manual_changes_md([]) == ""

    def test_includes_target_file_and_rationale(self):
        md = render_manual_changes_md([{
            "target_file": "pulse_categories.py",
            "description": "lower baustellen weight",
            "rationale": "4 overrides this week",
            "diff": "0.5 -> 0.3",
        }])
        assert "pulse_categories.py" in md
        assert "lower baustellen weight" in md
        assert "4 overrides this week" in md
        assert "0.5 -> 0.3" in md
