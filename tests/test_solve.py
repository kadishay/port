from unittest.mock import patch, MagicMock
from agent.models import BugContext, Severity, AutonomyDecision
from agent.cost_tracker import CostTracker


def _ctx(**kwargs):
    defaults = dict(
        issue_number=42, issue_title="Overdue bug", issue_body="...",
        repo_path="/tmp", severity=Severity.HIGH, confidence=0.92,
        root_cause="time.Hour*38 too large",
    )
    return BugContext(**{**defaults, **kwargs})


def _end_turn(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.content = [block]
    return r


@patch("agent.solve.evaluate_autonomy", return_value=(AutonomyDecision.AUTO_MERGE, ["all criteria met"]))
@patch("agent.solve.git_diff", return_value="diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
@patch("agent.solve.run_shell", return_value=("ok", "", 0))
@patch("agent.solve.client")
def test_auto_merge_creates_pr(mock_client, mock_shell, mock_diff, mock_autonomy):
    from agent.solve import run_solve
    mock_client.messages.create.return_value = _end_turn(
        "Apply fix: change time.Hour*38 to time.Hour*14 in task_overdue_reminder.go"
    )
    mock_gh = MagicMock()
    ctx = _ctx()
    result = run_solve(ctx, mock_gh, CostTracker())
    assert result.autonomy_decision == AutonomyDecision.AUTO_MERGE
    mock_gh.create_pr.assert_called_once()


@patch("agent.solve.evaluate_autonomy", return_value=(AutonomyDecision.HITL_REQUIRED, ["CRITICAL severity"]))
@patch("agent.solve.wait_for_approval", return_value=False)
@patch("agent.solve.git_diff", return_value="diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
@patch("agent.solve.run_shell", return_value=("ok", "", 0))
@patch("agent.solve.client")
def test_hitl_rejected_skips_pr(mock_client, mock_shell, mock_diff, mock_approval, mock_autonomy):
    from agent.solve import run_solve
    mock_client.messages.create.return_value = _end_turn("Apply this fix.")
    mock_gh = MagicMock()
    ctx = _ctx(severity=Severity.CRITICAL)
    result = run_solve(ctx, mock_gh, CostTracker())
    mock_gh.create_pr.assert_not_called()
    mock_gh.post_comment.assert_called()
