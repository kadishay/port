from agent.autonomy import evaluate_autonomy
from agent.models import BugContext, Severity, AutonomyDecision


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

MULTI_FILE_DIFF = SMALL_DIFF + """
diff --git a/pkg/models/task.go b/pkg/models/task.go
index abc..def 100644
--- a/pkg/models/task.go
+++ b/pkg/models/task.go
@@ -1,1 +1,1 @@
-foo
+bar
diff --git a/pkg/models/task_search.go b/pkg/models/task_search.go
index abc..def 100644
--- a/pkg/models/task_search.go
+++ b/pkg/models/task_search.go
@@ -1,1 +1,1 @@
-baz
+qux
"""


def test_auto_merge_when_all_criteria_met():
    decision, reasons = evaluate_autonomy(_ctx(), SMALL_DIFF)
    assert decision == AutonomyDecision.AUTO_MERGE
    assert any("all autonomy criteria met" in r for r in reasons)


def test_hitl_for_critical():
    decision, _ = evaluate_autonomy(_ctx(severity=Severity.CRITICAL), SMALL_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED


def test_hitl_for_medium_severity():
    decision, _ = evaluate_autonomy(_ctx(severity=Severity.MEDIUM), SMALL_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED


def test_hitl_for_low_confidence():
    decision, reasons = evaluate_autonomy(_ctx(confidence=0.70), SMALL_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED
    assert any("confidence" in r for r in reasons)


def test_hitl_for_large_diff():
    decision, reasons = evaluate_autonomy(_ctx(), LARGE_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED
    assert any("lines" in r for r in reasons)


def test_hitl_for_too_many_files():
    decision, reasons = evaluate_autonomy(_ctx(), MULTI_FILE_DIFF)
    assert decision == AutonomyDecision.HITL_REQUIRED
    assert any("files" in r for r in reasons)


def test_escalate_for_auth_changes():
    diff_with_auth = SMALL_DIFF + "\n+// auth token handling\n"
    decision, reasons = evaluate_autonomy(_ctx(), diff_with_auth)
    assert decision == AutonomyDecision.ESCALATE_ONLY
    assert any("auth" in r for r in reasons)
