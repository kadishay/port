import json
import anthropic
from agent.models import BugContext, RiskLevel, Severity, AutonomyDecision

_client = anthropic.Anthropic()

_ESCALATE_KEYWORDS = ["auth token", "auth middleware", "session token", "migration", "schema alter"]
_LOW_MAX_FILES = 2
_LOW_MAX_LINES = 15
_LOW_MIN_CONFIDENCE = 0.85
_MED_MAX_FILES = 5
_MED_MAX_LINES = 50
_MED_MIN_CONFIDENCE = 0.70

_RISK_ORDER = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.ESCALATE: 3}


def evaluate_risk(diff: str, ctx: BugContext) -> tuple[RiskLevel, list[str]]:
    """Assess how dangerous applying this fix is, independent of severity."""
    rule_risk, reasons = _rule_risk(diff, ctx)
    model_risk, model_reason = _model_risk(diff, ctx)

    if _RISK_ORDER[model_risk] > _RISK_ORDER[rule_risk]:
        reasons.append(f"Haiku raised risk to {model_risk}: {model_reason}")
        return model_risk, reasons

    reasons.append(f"Haiku agreed: {model_reason}")
    return rule_risk, reasons


def _rule_risk(diff: str, ctx: BugContext) -> tuple[RiskLevel, list[str]]:
    """Deterministic rule-based floor — keyword matching + size + confidence."""
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
            f"{changed_files} file(s), {changed_lines} line(s), confidence {ctx.confidence:.2f} — LOW risk (rules)"
        )
        return RiskLevel.LOW, reasons

    if (
        changed_files <= _MED_MAX_FILES
        and changed_lines <= _MED_MAX_LINES
        and ctx.confidence >= _MED_MIN_CONFIDENCE
    ):
        reasons.append(
            f"{changed_files} file(s), {changed_lines} line(s), confidence {ctx.confidence:.2f} — MEDIUM risk (rules)"
        )
        return RiskLevel.MEDIUM, reasons

    if changed_files > _MED_MAX_FILES:
        reasons.append(f"{changed_files} files changed — HIGH risk (limit {_MED_MAX_FILES})")
    if changed_lines > _MED_MAX_LINES:
        reasons.append(f"{changed_lines} lines changed — HIGH risk (limit {_MED_MAX_LINES})")
    if ctx.confidence < _MED_MIN_CONFIDENCE:
        reasons.append(f"confidence {ctx.confidence:.2f} below {_MED_MIN_CONFIDENCE} — HIGH risk")
    return RiskLevel.HIGH, reasons


def _model_risk(diff: str, ctx: BugContext) -> tuple[RiskLevel, str]:
    """Haiku semantic risk assessment — can only raise the rule-based floor, never lower it."""
    response = _client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Assess the risk of applying this code fix. Focus on semantic danger:\n"
                f"- Does it remove a safety check or null guard?\n"
                f"- Does it touch shared utilities used across many callers?\n"
                f"- Does it change a default value with broad effect?\n"
                f"- Does it alter control flow in a non-obvious way?\n"
                f"- Does the function/variable name suggest it handles auth, permissions, or data integrity?\n\n"
                f"Issue: {ctx.issue_title}\n"
                f"Root cause: {ctx.root_cause}\n\n"
                f"Diff:\n{diff[:3000]}\n\n"
                'Respond with JSON only: {"risk": "LOW|MEDIUM|HIGH|ESCALATE", "reason": "one sentence"}'
            ),
        }],
    )
    text = next(b.text for b in response.content if b.type == "text")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        return RiskLevel(data["risk"]), data.get("reason", "")
    except (json.JSONDecodeError, KeyError, ValueError):
        return RiskLevel.LOW, "model response unparseable — defaulting to LOW"


def evaluate_autonomy(ctx: BugContext, diff: str) -> tuple[AutonomyDecision, list[str]]:
    """Combine severity + risk into a final merge decision."""
    risk, risk_reasons = evaluate_risk(diff, ctx)
    ctx.risk_level = risk
    ctx.risk_reasons = risk_reasons

    reasons: list[str] = list(risk_reasons)

    if risk == RiskLevel.ESCALATE:
        reasons.append(
            "ESCALATE risk (auth/security-sensitive) — fix still suggested, "
            "human approval required, never auto-merged"
        )
        return AutonomyDecision.HITL_REQUIRED, reasons

    if ctx.severity == Severity.CRITICAL:
        reasons.append("CRITICAL severity — HITL always required regardless of risk")
        return AutonomyDecision.HITL_REQUIRED, reasons

    # Risk is the sole driver of the merge decision
    if risk == RiskLevel.LOW:
        reasons.append("LOW risk — auto PR")
        return AutonomyDecision.AUTO_PR, reasons

    reasons.append(f"{risk} risk — HITL required")
    return AutonomyDecision.HITL_REQUIRED, reasons


def _count_files(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line.startswith("diff --git"))


def _count_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )
