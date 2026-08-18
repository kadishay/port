from agent.models import BugContext, Severity, AutonomyDecision

_ESCALATE_KEYWORDS = ["auth token", "auth middleware", "session token", "migration", "schema alter"]
_MAX_FILES = 2
_MAX_LINES = 30
_MIN_CONFIDENCE = 0.85


def evaluate_autonomy(ctx: BugContext, diff: str) -> tuple[AutonomyDecision, list[str]]:
    reasons: list[str] = []

    for kw in _ESCALATE_KEYWORDS:
        if kw in diff.lower():
            reasons.append(f"diff contains '{kw}' — escalate only, no auto-fix")
            return AutonomyDecision.ESCALATE_ONLY, reasons

    if ctx.severity == Severity.CRITICAL:
        reasons.append("CRITICAL severity — HITL always required")
        return AutonomyDecision.HITL_REQUIRED, reasons

    if ctx.severity != Severity.HIGH:
        reasons.append(f"{ctx.severity} severity — auto-fix only for HIGH")
        return AutonomyDecision.HITL_REQUIRED, reasons

    changed_files = _count_files(diff)
    if changed_files > _MAX_FILES:
        reasons.append(f"{changed_files} files changed — exceeds limit of {_MAX_FILES}")
        return AutonomyDecision.HITL_REQUIRED, reasons

    changed_lines = _count_lines(diff)
    if changed_lines > _MAX_LINES:
        reasons.append(f"{changed_lines} lines changed — exceeds limit of {_MAX_LINES}")
        return AutonomyDecision.HITL_REQUIRED, reasons

    if ctx.confidence < _MIN_CONFIDENCE:
        reasons.append(f"confidence {ctx.confidence:.2f} below threshold {_MIN_CONFIDENCE}")
        return AutonomyDecision.HITL_REQUIRED, reasons

    reasons.append("all autonomy criteria met")
    return AutonomyDecision.AUTO_MERGE, reasons


def _count_files(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith("diff --git"))


def _count_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
