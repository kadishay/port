import random

from agent.models import AutonomyDecision, BugContext, RiskLevel
from agent.cost_tracker import CostTracker

_OPUS_MODEL_KEY = "claude-opus-4-8"


def derive_bug_type(affected_files: list[str]) -> str:
    if not affected_files:
        return "UNKNOWN"
    return "FE" if any(f.startswith("frontend/") for f in affected_files) else "BE"


def derive_pipeline_outcome(ctx: BugContext, crashed: bool) -> str:
    if crashed:
        return "crashed"
    if ctx.not_a_bug:
        return "not_a_bug"
    if ctx.unable_to_reproduce:
        return "unable_to_reproduce"
    if ctx.pr_url:
        return "fixed_auto_pr"
    if ctx.autonomy_decision == AutonomyDecision.HITL_REQUIRED:
        return "hitl_rejected"
    return "hitl_pending"


def mock_human_outcomes(risk_level: RiskLevel, autonomy_decision: AutonomyDecision) -> dict:
    low_risk = (
        autonomy_decision == AutonomyDecision.AUTO_PR
        and risk_level in (RiskLevel.LOW, RiskLevel.MEDIUM)
    )
    reject_p = 0.05 if low_risk else 0.35
    merge_p = 0.85 if low_risk else 0.30
    comment_p = 0.15 if low_risk else 0.45

    rejected = random.random() < reject_p
    merged_as_is = random.random() < merge_p and not rejected
    added_comment = random.random() < comment_p

    return {
        "human_rejected": rejected,
        "human_merged_as_is": merged_as_is,
        "human_added_comment": added_comment,
    }


def build_row(ctx: BugContext, tracker: CostTracker, duration_seconds: float, crashed: bool) -> dict:
    row = {
        "issue_number": ctx.issue_number,
        "issue_title": ctx.issue_title,
        "bug_type": derive_bug_type(ctx.affected_files),
        "duration_seconds": duration_seconds,
        "cost_usd": tracker.total_cost(),
        "severity": ctx.severity.value,
        "risk_level": ctx.risk_level.value,
        "risk_reasons": ctx.risk_reasons,
        "autonomy_decision": ctx.autonomy_decision.value,
        "opus_fallback_used": _OPUS_MODEL_KEY in tracker.models_used(),
        "pipeline_outcome": derive_pipeline_outcome(ctx, crashed),
        "pr_url": ctx.pr_url or None,
    }
    row.update(mock_human_outcomes(ctx.risk_level, ctx.autonomy_decision))
    return row
