"""Applies a review report's changes.json to the working tree (Phase 4,
docs/review.md#opening-the-pr) — run by the review-pr CI workflow only,
never by the prod server. A unified diff is applied via `git apply`;
anything else (freeform old->new description) is written out to
PROPOSED_CHANGES.md for manual application — a proposed edit is never
silently dropped.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def is_unified_diff(diff_text: str) -> bool:
    text = (diff_text or "").lstrip()
    return text.startswith("--- ") or text.startswith("diff --git") or text.startswith("@@ ") or "\n@@ " in text


def render_manual_changes_md(manual_changes: list[dict]) -> str:
    if not manual_changes:
        return ""
    lines = [
        "# Manual changes to apply",
        "",
        "These proposed edits from the City Pulse review were not a parseable "
        "unified diff (or failed to apply cleanly) — apply by hand.",
        "",
    ]
    for c in manual_changes:
        lines.append(f"## {c.get('target_file', '(unknown file)')}")
        lines.append("")
        if c.get("description"):
            lines.append(c["description"])
            lines.append("")
        if c.get("rationale"):
            lines.append(f"**Rationale:** {c['rationale']}")
            lines.append("")
        lines.append("```")
        lines.append(c.get("diff", ""))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)


def apply_changes(changes: list[dict], repo_root: Path) -> tuple[list[dict], list[dict]]:
    """Split `changes` into (applied, manual).

    Attempts `git apply` for each unified-diff-shaped change against
    `repo_root`. A change that isn't diff-shaped, or fails to apply cleanly,
    falls back to `manual` rather than being dropped.
    """
    applied: list[dict] = []
    manual: list[dict] = []

    for change in changes:
        diff_text = change.get("diff", "")
        if not is_unified_diff(diff_text):
            manual.append(change)
            continue

        patch_path = repo_root / ".review_patch.diff"
        patch_path.write_text(diff_text if diff_text.endswith("\n") else diff_text + "\n", encoding="utf-8")
        try:
            subprocess.run(
                ["git", "apply", "--whitespace=fix", str(patch_path)],
                cwd=repo_root, check=True, capture_output=True, text=True,
            )
            applied.append(change)
        except subprocess.CalledProcessError:
            manual.append(change)
        finally:
            patch_path.unlink(missing_ok=True)

    return applied, manual


def main() -> None:
    changes_path = Path(sys.argv[1])
    repo_root = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(".")

    data = json.loads(changes_path.read_text(encoding="utf-8"))
    changes = data.get("changes", [])
    applied, manual = apply_changes(changes, repo_root)

    manual_md = render_manual_changes_md(manual)
    if manual_md:
        (repo_root / "PROPOSED_CHANGES.md").write_text(manual_md, encoding="utf-8")

    (repo_root / ".review_apply_summary.json").write_text(
        json.dumps({"applied": applied, "manual": manual}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"applied={len(applied)} manual={len(manual)}")


if __name__ == "__main__":
    main()
