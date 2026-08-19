import random
from agent.models import BugContext, Severity, RiskLevel, AutonomyDecision
from agent.cost_tracker import CostTracker
from agent.telemetry import (
    derive_bug_type,
    derive_pipeline_outcome,
    mock_human_outcomes,
    build_row,
)


def _ctx(**kwargs) -> BugContext:
    defaults = dict(
        issue_number=1, issue_title="Bug", issue_body="...",
        repo_path="/tmp",
    )
    return BugContext(**{**defaults, **kwargs})


def test_derive_bug_type_frontend():
    assert derive_bug_type(["frontend/src/stores/kanban.ts"]) == "FE"


def test_derive_bug_type_backend():
    assert derive_bug_type(["pkg/models/task_overdue_reminder.go"]) == "BE"


def test_derive_bug_type_mixed_files_is_frontend_if_any_frontend_file():
    assert derive_bug_type(["pkg/models/x.go", "frontend/src/y.ts"]) == "FE"


def test_derive_bug_type_unknown_when_empty():
    assert derive_bug_type([]) == "UNKNOWN"


def test_derive_pipeline_outcome_crashed_takes_priority():
    ctx = _ctx(not_a_bug=True)
    assert derive_pipeline_outcome(ctx, crashed=True) == "crashed"


def test_derive_pipeline_outcome_not_a_bug():
    ctx = _ctx(not_a_bug=True)
    assert derive_pipeline_outcome(ctx, crashed=False) == "not_a_bug"


def test_derive_pipeline_outcome_unable_to_reproduce():
    ctx = _ctx(unable_to_reproduce=True)
    assert derive_pipeline_outcome(ctx, crashed=False) == "unable_to_reproduce"


def test_derive_pipeline_outcome_fixed_auto_pr_when_pr_url_set():
    ctx = _ctx(pr_url="https://github.com/x/y/pull/1")
    assert derive_pipeline_outcome(ctx, crashed=False) == "fixed_auto_pr"


def test_derive_pipeline_outcome_hitl_rejected_when_hitl_and_no_pr():
    ctx = _ctx(autonomy_decision=AutonomyDecision.HITL_REQUIRED, pr_url="")
    assert derive_pipeline_outcome(ctx, crashed=False) == "hitl_rejected"


def test_derive_pipeline_outcome_hitl_pending_fallback():
    ctx = _ctx(autonomy_decision=AutonomyDecision.AUTO_PR, pr_url="")
    assert derive_pipeline_outcome(ctx, crashed=False) == "hitl_pending"


def test_mock_human_outcomes_low_risk_auto_pr_favors_merge(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.5)
    outcome = mock_human_outcomes(RiskLevel.LOW, AutonomyDecision.AUTO_PR)
    assert outcome == {
        "human_rejected": False,
        "human_merged_as_is": True,
        "human_added_comment": False,
    }


def test_mock_human_outcomes_high_risk_hitl_favors_rejection(monkeypatch):
    values = iter([0.1, 0.9, 0.1])
    monkeypatch.setattr(random, "random", lambda: next(values))
    outcome = mock_human_outcomes(RiskLevel.HIGH, AutonomyDecision.HITL_REQUIRED)
    assert outcome == {
        "human_rejected": True,
        "human_merged_as_is": False,
        "human_added_comment": True,
    }


def test_mock_human_outcomes_rejected_forces_merged_as_is_false(monkeypatch):
    values = iter([0.01, 0.01, 0.9])
    monkeypatch.setattr(random, "random", lambda: next(values))
    outcome = mock_human_outcomes(RiskLevel.LOW, AutonomyDecision.AUTO_PR)
    assert outcome["human_rejected"] is True
    assert outcome["human_merged_as_is"] is False


def test_build_row_includes_all_fields(monkeypatch):
    monkeypatch.setattr(random, "random", lambda: 0.99)
    ctx = _ctx(
        severity=Severity.HIGH,
        risk_level=RiskLevel.LOW,
        risk_reasons=["small diff"],
        autonomy_decision=AutonomyDecision.AUTO_PR,
        pr_url="https://github.com/x/y/pull/9",
        affected_files=["pkg/models/task_overdue_reminder.go"],
    )
    tracker = CostTracker()
    row = build_row(ctx, tracker, duration_seconds=12.5, crashed=False)

    assert row["issue_number"] == 1
    assert row["issue_title"] == "Bug"
    assert row["bug_type"] == "BE"
    assert row["duration_seconds"] == 12.5
    assert row["cost_usd"] == 0.0
    assert row["severity"] == "HIGH"
    assert row["risk_level"] == "LOW"
    assert row["risk_reasons"] == ["small diff"]
    assert row["autonomy_decision"] == "AUTO_PR"
    assert row["opus_fallback_used"] is False
    assert row["pipeline_outcome"] == "fixed_auto_pr"
    assert row["pr_url"] == "https://github.com/x/y/pull/9"
    assert {"human_rejected", "human_merged_as_is", "human_added_comment"} <= row.keys()


def test_build_row_pr_url_is_none_when_empty():
    ctx = _ctx()
    tracker = CostTracker()
    row = build_row(ctx, tracker, duration_seconds=1.0, crashed=False)
    assert row["pr_url"] is None


def test_build_row_opus_fallback_used_true_when_opus_recorded():
    ctx = _ctx()
    tracker = CostTracker()
    from unittest.mock import MagicMock
    tracker.record("claude-opus-4-8-20260101", MagicMock(input_tokens=1, output_tokens=1))
    row = build_row(ctx, tracker, duration_seconds=1.0, crashed=False)
    assert row["opus_fallback_used"] is True
