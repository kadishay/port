from agent.models import BugContext, Severity, AutonomyDecision


def test_bug_context_defaults():
    ctx = BugContext(issue_number=1, issue_title="Bug", issue_body="...", repo_path="/tmp")
    assert ctx.severity == Severity.MEDIUM
    assert ctx.confidence == 0.0
    assert ctx.autonomy_decision == AutonomyDecision.HITL_REQUIRED
    assert ctx.autonomy_reasons == []


def test_severity_enum_values():
    assert Severity.CRITICAL == "CRITICAL"
    assert Severity.HIGH == "HIGH"
    assert Severity.MEDIUM == "MEDIUM"
    assert Severity.LOW == "LOW"


def test_autonomy_decision_enum_values():
    assert AutonomyDecision.AUTO_PR == "AUTO_PR"
    assert AutonomyDecision.HITL_REQUIRED == "HITL_REQUIRED"
