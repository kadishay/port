from agent.autonomy import evaluate_autonomy, evaluate_risk
from agent.models import BugContext, Severity, RiskLevel, AutonomyDecision


def _ctx(**kwargs) -> BugContext:
    defaults = dict(
        issue_number=1, issue_title="Bug", issue_body="...",
        repo_path="/tmp", severity=Severity.HIGH, confidence=0.90,
    )
    return BugContext(**{**defaults, **kwargs})


SMALL_DIFF = """\
diff --git a/pkg/models/task_overdue_reminder.go b/pkg/models/task_overdue_reminder.go
index abc..def 100644
--- a/pkg/models/task_overdue_reminder.go
+++ b/pkg/models/task_overdue_reminder.go
@@ -41,1 +41,1 @@
-\t\t\t\t\tnextMinute.Add(time.Hour*38).Format(dbTimeFormat)).
+\t\t\t\t\tnextMinute.Add(time.Hour*14).Format(dbTimeFormat)).
"""

_LARGE_LINES = "\n".join(f"-old{i}\n+new{i}" for i in range(40))
LARGE_DIFF = f"""\
diff --git a/pkg/models/task_overdue_reminder.go b/pkg/models/task_overdue_reminder.go
index abc..def 100644
--- a/pkg/models/task_overdue_reminder.go
+++ b/pkg/models/task_overdue_reminder.go
@@ -1,40 +1,40 @@
{_LARGE_LINES}
"""

MULTI_FILE_DIFF = SMALL_DIFF + "\n".join(
    f"""
diff --git a/pkg/models/task_{i}.go b/pkg/models/task_{i}.go
index abc..def 100644
--- a/pkg/models/task_{i}.go
+++ b/pkg/models/task_{i}.go
@@ -1,1 +1,1 @@
-foo
+bar"""
    for i in range(5)
)


# ---------------------------------------------------------------------------
# evaluate_risk tests
# ---------------------------------------------------------------------------

def test_risk_low_for_small_diff():
    risk, _ = evaluate_risk(SMALL_DIFF, _ctx(confidence=0.90))
    assert risk == RiskLevel.LOW


def test_risk_high_for_large_diff():
    risk, reasons = evaluate_risk(LARGE_DIFF, _ctx(confidence=0.90))
    assert risk == RiskLevel.HIGH
    assert any("lines" in r for r in reasons)


def test_risk_high_for_many_files():
    risk, reasons = evaluate_risk(MULTI_FILE_DIFF, _ctx(confidence=0.90))
    assert risk == RiskLevel.HIGH
    assert any("files" in r for r in reasons)


def test_risk_high_for_low_confidence():
    risk, reasons = evaluate_risk(SMALL_DIFF, _ctx(confidence=0.60))
    assert risk == RiskLevel.HIGH
    assert any("confidence" in r for r in reasons)


def test_risk_medium_for_medium_confidence():
    # confidence 0.75 is in medium band; small diff keeps file/line count LOW
    risk, _ = evaluate_risk(SMALL_DIFF, _ctx(confidence=0.75))
    assert risk == RiskLevel.MEDIUM


def test_risk_escalate_for_auth_keyword():
    diff_with_auth = SMALL_DIFF + "\n+// auth token handling\n"
    risk, reasons = evaluate_risk(diff_with_auth, _ctx())
    assert risk == RiskLevel.ESCALATE
    assert any("auth" in r for r in reasons)


# ---------------------------------------------------------------------------
# evaluate_autonomy tests (severity × risk matrix)
# ---------------------------------------------------------------------------

def test_auto_merge_high_severity_low_risk():
    decision, reasons = evaluate_autonomy(_ctx(severity=Severity.HIGH, confidence=0.90), SMALL_DIFF)
    assert decision == AutonomyDecision.AUTO_MERGE
    assert any("LOW risk" in r for r in reasons)


def test_auto_merge_medium_severity_low_risk():
    decision, _ = evaluate_autonomy(_ctx(severity=Severity.MEDIUM, confidence=0.90), SMALL_DIFF)
    assert decision == AutonomyDecision.AUTO_MERGE


def test_auto_merge_low_severity_low_risk():
    decision, _ = evaluate_autonomy(_ctx(severity=Severity.LOW, confidence=0.90), SMALL_DIFF)
    assert decision == AutonomyDecision.AUTO_MERGE


def test_hitl_for_critical_even_low_risk():
    decision, reasons = evaluate_autonomy(_ctx(severity=Severity.CRITICAL, confidence=0.90), SMALL_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED
    assert any("CRITICAL" in r for r in reasons)


def test_hitl_for_medium_risk():
    decision, _ = evaluate_autonomy(_ctx(severity=Severity.HIGH, confidence=0.75), SMALL_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED


def test_hitl_for_large_diff():
    decision, reasons = evaluate_autonomy(_ctx(), LARGE_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED
    assert any("lines" in r for r in reasons)


def test_escalate_for_auth_changes():
    diff_with_auth = SMALL_DIFF + "\n+// auth token handling\n"
    decision, reasons = evaluate_autonomy(_ctx(), diff_with_auth)
    assert decision == AutonomyDecision.ESCALATE_ONLY
    assert any("auth" in r for r in reasons)


def test_risk_stored_on_ctx():
    ctx = _ctx(severity=Severity.HIGH, confidence=0.90)
    evaluate_autonomy(ctx, SMALL_DIFF)
    assert ctx.risk_level == RiskLevel.LOW
    assert ctx.risk_reasons
