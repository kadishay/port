"""
Integration tests for the two demo bugs.

These run the full triage + solve pipeline against the real Vikunja codebase
using real LLM API calls. GitHub API calls and git push are mocked so nothing
is pushed to the remote. Playwright is always disabled for speed.

Each test takes ~40-60 seconds. Both together run in under 2 minutes.

These run as a pre-commit hook — see .git/hooks/pre-commit.
"""

import os
import pytest
from unittest.mock import MagicMock, patch
from dotenv import load_dotenv

load_dotenv(override=True)

from agent.models import BugContext, Severity, AutonomyDecision
from agent.triage import run_triage
from agent.solve import run_solve
from agent.cost_tracker import CostTracker
from agent.tools.shell_tools import run_shell

VIKUNJA_REPO = os.environ.get("VIKUNJA_REPO_PATH", "/Users/kadishay/Code/vikunja")

# Canned issue payloads — same wording a developer would actually file
_BE_ISSUE = {
    "number": 9901,
    "title": "Bug: Overdue reminder fires for completed tasks",
    "body": (
        "Steps to reproduce:\n"
        "1. Create a task with a due date in the past.\n"
        "2. Mark it as done.\n"
        "3. Wait for the overdue reminder cron (runs every minute).\n\n"
        "Expected: No reminder sent for tasks already marked done.\n"
        "Actual: Reminder still fires for completed tasks while truly overdue\n"
        "pending tasks receive no notification."
    ),
}

_FE_ISSUE = {
    "number": 9902,
    "title": "Bug: Marking a task as done in Kanban view doesn't move it to Done column",
    "body": (
        "Steps to reproduce:\n"
        "1. Open the Kanban view for a project that has a Done bucket.\n"
        "2. Create a task in a non-done bucket.\n"
        "3. Click the task to open it, then click the Mark Done button.\n"
        "4. Use the browser back button to return to the Kanban board.\n\n"
        "Expected: Task has moved into the Done column.\n"
        "Actual: Task is still in its original column, but now shows a Done badge.\n"
        "The condition that moves the task appears to be inverted."
    ),
}


_UNREPRODUCIBLE_ISSUE = {
    "number": 9903,
    "title": "Bug: Set color is not working",
    "body": (
        "Setting a task color is not working.\n\n"
        "Steps to reproduce:\n"
        "1. Open a task.\n"
        "2. Click set color.\n"
        "3. Click on a color selector circle.\n"
        "4. Select red color.\n"
        "5. Go back to project.\n\n"
        "Expected behaviour: red dot will appear next to the task.\n"
        "Actual behaviour: no dot appears next to the task."
    ),
}


def _mock_gh(issue_data: dict) -> MagicMock:
    gh = MagicMock()
    gh.get_issue.return_value = issue_data
    gh.post_comment.return_value = None
    gh.add_label.return_value = None
    gh.close_issue.return_value = None
    gh.create_pr.return_value = {"html_url": "https://github.com/test/vikunja/pull/999"}
    gh.get_commit_author_login.return_value = None
    gh.get_file_top_authors.return_value = []
    return gh


@pytest.fixture
def no_playwright(monkeypatch):
    """Disable browser — used for the backend test where a browser adds no value."""
    monkeypatch.setenv("PLAYWRIGHT_ENABLED", "false")



@pytest.fixture
def clean_vikunja():
    """Return vikunja to main after each test, even on failure."""
    yield
    run_shell(f"git -C {VIKUNJA_REPO} checkout -- .")
    run_shell(f"git -C {VIKUNJA_REPO} checkout main")
    for branch in ["fix/issue-9901", "fix/issue-9902"]:
        run_shell(f"git -C {VIKUNJA_REPO} branch -D {branch}")  # no-op if branch doesn't exist


def _diff_for_branch(branch: str) -> str:
    out, _, _ = run_shell(f"git -C {VIKUNJA_REPO} diff main..{branch}")
    return out


@patch("agent.solve.wait_for_approval", side_effect=AssertionError(
    "HITL triggered — expected AUTO_PR for the backend demo bug"
))
@patch("agent.solve._push_and_open_pr", side_effect=lambda ctx, gh: ctx)
def test_backend_bug_overdue_reminder(mock_push, mock_wait, clean_vikunja, no_playwright):
    """
    Backend bug: And("done = true") in task_overdue_reminder.go should be And("done = false").

    The agent must:
    - Classify as backend / HIGH severity
    - Identify task_overdue_reminder.go as the affected file
    - Fix done = true → done = false (the exact one-character boolean flip)
    - Qualify for AUTO_PR (single-file, single-line, HIGH severity)
    """

    tracker = CostTracker()
    gh = _mock_gh(_BE_ISSUE)
    ctx = BugContext(
        issue_number=9901,
        issue_title=_BE_ISSUE["title"],
        issue_body=_BE_ISSUE["body"],
        repo_path=VIKUNJA_REPO,
    )

    ctx = run_triage(ctx, gh, tracker)

    # --- Triage assertions ---
    assert ctx.severity == Severity.HIGH, f"Expected HIGH, got {ctx.severity}"
    assert ctx.confidence >= 0.80, f"Confidence too low: {ctx.confidence}"
    assert any(
        "overdue" in f or "reminder" in f for f in ctx.affected_files
    ), f"Didn't find overdue/reminder file: {ctx.affected_files}"
    assert ctx.buggy_pattern, "buggy_pattern should not be empty"
    # The agent should identify 'true' as the wrong value (should be 'false')
    pattern_lower = ctx.buggy_pattern.lower()
    assert "true" in pattern_lower or "done" in pattern_lower, (
        f"buggy_pattern doesn't mention 'true' or 'done': {ctx.buggy_pattern!r}"
    )

    ctx = run_solve(ctx, gh, tracker)

    # --- Solve assertions ---
    assert ctx.autonomy_decision == AutonomyDecision.AUTO_PR, (
        f"Expected AUTO_PR, got {ctx.autonomy_decision}. Reasons: {ctx.autonomy_reasons}"
    )

    diff = _diff_for_branch("fix/issue-9901")
    assert diff, "No diff produced — solve made no file changes"
    assert '-And("done = true")' in diff or '-\t\tAnd("done = true")' in diff, (
        f"Diff doesn't remove 'done = true':\n{diff}"
    )
    assert '+And("done = false")' in diff or '+\t\tAnd("done = false")' in diff, (
        f"Diff doesn't add 'done = false':\n{diff}"
    )

    print(f"\n[BE test] cost: {tracker.summary()}")
    print(f"[BE test] root_cause: {ctx.root_cause}")
    print(f"[BE test] buggy_pattern: {ctx.buggy_pattern}")
    print(f"[BE test] diff:\n{diff}")


@patch("agent.solve.wait_for_approval", return_value=True)
@patch("agent.solve._push_and_open_pr", side_effect=lambda ctx, gh: ctx)
def test_frontend_bug_kanban_done_bucket(mock_push, mock_wait, clean_vikunja, no_playwright):
    """
    Frontend bug: currentTaskBucket.id === currentView.doneBucketId should be !==.

    The agent must:
    - Classify as frontend / HIGH severity
    - Identify kanban.ts as the affected file
    - Fix === → !== (the single operator flip in ensureTaskIsInCorrectBucket)

    Confidence threshold is 0.60 (not 0.85): kanban.ts has two nearly identical
    conditions (lines 173 and 178) that look the same statically. Without visual
    proof the model correctly hedges — confidence 0.60-0.80 is expected and normal.
    The diff assertion is the real ground truth for correctness.

    HITL is allowed (wait_for_approval mocked to return True) since confidence
    may fall below 0.85. What matters is the agent identifies the right file and
    applies the right fix, regardless of autonomy path.
    """

    tracker = CostTracker()
    gh = _mock_gh(_FE_ISSUE)
    ctx = BugContext(
        issue_number=9902,
        issue_title=_FE_ISSUE["title"],
        issue_body=_FE_ISSUE["body"],
        repo_path=VIKUNJA_REPO,
    )

    ctx = run_triage(ctx, gh, tracker)

    # --- Triage assertions ---
    assert ctx.severity == Severity.HIGH, f"Expected HIGH, got {ctx.severity}"
    # 0.60 is realistic: two nearly identical conditions in kanban.ts give the
    # model insufficient static signal to distinguish them with full confidence.
    assert ctx.confidence >= 0.60, f"Confidence too low: {ctx.confidence}"
    assert any(
        "kanban" in f for f in ctx.affected_files
    ), f"Didn't find kanban file: {ctx.affected_files}"

    ctx = run_solve(ctx, gh, tracker)

    diff = _diff_for_branch("fix/issue-9902")
    assert diff, "No diff produced — solve made no file changes"
    # Must remove the === condition and add !==
    assert "=== currentView.doneBucketId" in diff or "===currentView.doneBucketId" in diff, (
        f"Diff doesn't show === being removed:\n{diff}"
    )
    assert "!== currentView.doneBucketId" in diff or "!==currentView.doneBucketId" in diff, (
        f"Diff doesn't show !== being added:\n{diff}"
    )
    # Must NOT have changed line 178 (the !done branch — that === is correct)
    diff_lines = diff.splitlines()
    changed = [l for l in diff_lines if l.startswith("+") or l.startswith("-")]
    assert sum(1 for l in changed if "doneBucketId" in l) <= 2, (
        f"Too many doneBucketId lines changed — agent may have touched the wrong condition:\n{diff}"
    )

    print(f"\n[FE test] cost: {tracker.summary()}")
    print(f"[FE test] root_cause: {ctx.root_cause}")
    print(f"[FE test] buggy_pattern: {ctx.buggy_pattern}")
    print(f"[FE test] diff:\n{diff}")


def test_cannot_reproduce_color_bug(no_playwright):
    """
    Demo bug 3: "unable to reproduce". Unlike the other two demo bugs, this
    issue describes behaviour that does NOT actually exist in this codebase —
    setting a task color works correctly. The agent must:
    - Follow the reproduction steps (source-code inspection, no browser)
    - Correctly conclude it could NOT reproduce the described bug
    - Stop before root-cause analysis and solve entirely (no fabricated fix
      for something that isn't broken)

    Confidence-gated auto-close (>=0.85, see agent/triage.py) is intentionally
    NOT asserted here since it depends on real model confidence, which varies
    run to run — see tests/test_triage.py for a deterministic mocked test of
    that close-vs-leave-open branching. What matters in this integration test
    is the core detection capability against the real Vikunja codebase.
    """
    tracker = CostTracker()
    gh = _mock_gh(_UNREPRODUCIBLE_ISSUE)
    ctx = BugContext(
        issue_number=9903,
        issue_title=_UNREPRODUCIBLE_ISSUE["title"],
        issue_body=_UNREPRODUCIBLE_ISSUE["body"],
        repo_path=VIKUNJA_REPO,
    )

    ctx = run_triage(ctx, gh, tracker)

    assert ctx.unable_to_reproduce is True, (
        f"Expected unable_to_reproduce=True, got root_cause={ctx.root_cause!r} "
        f"reproduction_reason={ctx.reproduction_reason!r}"
    )
    assert ctx.reproduction_confidence >= 0.5, f"Confidence too low to be meaningful: {ctx.reproduction_confidence}"
    assert ctx.root_cause == "", "Should never have reached root-cause analysis"
    gh.add_label.assert_called_once_with(9903, "cannot-reproduce")
    gh.post_comment.assert_called_once()

    print(f"\n[unreproducible test] cost: {tracker.summary()}")
    print(f"[unreproducible test] confidence: {ctx.reproduction_confidence}")
    print(f"[unreproducible test] reason: {ctx.reproduction_reason}")
    print(f"[unreproducible test] issue_closed: {ctx.issue_closed}")
