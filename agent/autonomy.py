from agent.models import BugContext, RiskLevel, Severity, AutonomyDecision

_ESCALATE_KEYWORDS = ["auth token", "auth middleware", "session token", "migration", "schema alter"]
_LOW_MAX_FILES = 2
_LOW_MAX_LINES = 15
_LOW_MIN_CONFIDENCE = 0.85
_MED_MAX_FILES = 5
_MED_MAX_LINES = 50
_MED_MIN_CONFIDENCE = 0.70


def evaluate_risk(diff: str, ctx: BugContext) -> tuple[RiskLevel, list[str]]:
    """Assess how dangerous applying this fix is, independent of severity."""
    reasons: list[str] = []

    for kw in _ESCALATE_KEYWORDS:
        if kw in diff.lower():
            reasons.append(f"diff contains '{kw}' — escalate, no auto-fix")
            return RiskLevel.ESCALATE, reasons

    changed_files = _count_files(diff)
    changed_lines = _count_lines(diff)

    if (
        changed_files <= _LOW_MAX_FILES
        and changed_lines <= _LOW_MAX_LINES
        and ctx.confidence >= _LOW_MIN_CONFIDENCE
    ):
        reasons.append(
            f"{changed_files} file(s), {changed_lines} line(s), confidence {ctx.confidence:.2f} — LOW risk"
        )
        return RiskLevel.LOW, reasons

    if (
        changed_files <= _MED_MAX_FILES
        and changed_lines <= _MED_MAX_LINES
        and ctx.confidence >= _MED_MIN_CONFIDENCE
    ):
        reasons.append(
            f"{changed_files} file(s), {changed_lines} line(s), confidence {ctx.confidence:.2f} — MEDIUM risk"
        )
        return RiskLevel.MEDIUM, reasons

    if changed_files > _MED_MAX_FILES:
        reasons.append(f"{changed_files} files changed — HIGH risk (limit {_MED_MAX_FILES})")
    if changed_lines > _MED_MAX_LINES:
        reasons.append(f"{changed_lines} lines changed — HIGH risk (limit {_MED_MAX_LINES})")
    if ctx.confidence < _MED_MIN_CONFIDENCE:
        reasons.append(f"confidence {ctx.confidence:.2f} below {_MED_MIN_CONFIDENCE} — HIGH risk")
    return RiskLevel.HIGH, reasons


def evaluate_autonomy(ctx: BugContext, diff: str) -> tuple[AutonomyDecision, list[str]]:
    """Combine severity + risk into a final merge decision."""
    risk, risk_reasons = evaluate_risk(diff, ctx)
    ctx.risk_level = risk
    ctx.risk_reasons = risk_reasons

    reasons: list[str] = list(risk_reasons)

    if risk == RiskLevel.ESCALATE:
        return AutonomyDecision.ESCALATE_ONLY, reasons

    if ctx.severity == Severity.LOW:
        reasons.append("LOW severity — no auto-fix")
        return AutonomyDecision.HITL_REQUIRED, reasons

    if ctx.severity == Severity.CRITICAL:
        reasons.append("CRITICAL severity — HITL always required regardless of risk")
        return AutonomyDecision.HITL_REQUIRED, reasons

    # HIGH or MEDIUM severity: risk drives the merge decision
    if risk == RiskLevel.LOW:
        reasons.append("LOW risk + HIGH/MEDIUM severity — auto-merge")
        return AutonomyDecision.AUTO_MERGE, reasons

    reasons.append(f"{risk} risk — HITL required")
    return AutonomyDecision.HITL_REQUIRED, reasons


def _count_files(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith("diff --git"))


def _count_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
