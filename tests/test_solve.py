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


def _tool_use(name: str, input_dict: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_dict
    block.id = "tool_1"
    r = MagicMock()
    r.stop_reason = "tool_use"
    r.content = [block]
    r.model = "claude-haiku-4-5"
    return r


@patch("agent.solve.evaluate_autonomy", return_value=(AutonomyDecision.AUTO_PR, ["all criteria met"]))
@patch("agent.solve.git_diff", return_value="diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
@patch("agent.solve.run_shell", return_value=("ok", "", 0))
@patch("agent.solve.client")
def test_auto_pr_creates_pr(mock_client, mock_shell, mock_diff, mock_autonomy):
    from agent.solve import run_solve
    mock_client.messages.create.return_value = _end_turn(
        "Apply fix: change time.Hour*38 to time.Hour*14 in task_overdue_reminder.go"
    )
    mock_gh = MagicMock()
    ctx = _ctx()
    result = run_solve(ctx, mock_gh, CostTracker())
    assert result.autonomy_decision == AutonomyDecision.AUTO_PR
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


@patch("agent.solve.client")
def test_verify_fix_backend_low_confidence_retries_with_opus(mock_client):
    from agent.solve import _verify_fix

    mock_client.messages.create.side_effect = [
        _tool_use("report_verification", {"fixed": False, "confidence": 0.4, "evidence": "unsure"}),
        _tool_use("report_verification", {"fixed": True, "confidence": 0.9, "evidence": "confirmed fixed"}),
    ]
    ctx = _ctx(issue_title="Bug: overdue reminders fire for done tasks", reproduction_steps="curl ...")
    _verify_fix(ctx, CostTracker())

    assert ctx.fix_verified is True
    assert ctx.verification_confidence == 0.9
    assert mock_client.messages.create.call_count == 2
    first_model = mock_client.messages.create.call_args_list[0].kwargs["model"]
    second_model = mock_client.messages.create.call_args_list[1].kwargs["model"]
    assert first_model == "claude-haiku-4-5"
    assert second_model == "claude-opus-4-8"


@patch("agent.solve.client")
def test_verify_fix_backend_high_confidence_no_retry(mock_client):
    from agent.solve import _verify_fix

    mock_client.messages.create.return_value = _tool_use(
        "report_verification", {"fixed": True, "confidence": 0.95, "evidence": "confirmed fixed"}
    )
    ctx = _ctx(issue_title="Bug: overdue reminders fire for done tasks", reproduction_steps="curl ...")
    _verify_fix(ctx, CostTracker())

    assert ctx.fix_verified is True
    assert ctx.verification_confidence == 0.95
    assert mock_client.messages.create.call_count == 1
