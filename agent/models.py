from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AutonomyDecision(str, Enum):
    AUTO_MERGE = "AUTO_MERGE"
    HITL_REQUIRED = "HITL_REQUIRED"
    ESCALATE_ONLY = "ESCALATE_ONLY"


@dataclass
class BugContext:
    issue_number: int
    issue_title: str
    issue_body: str
    repo_path: str
    reproduction_log: str = ""
    root_cause: str = ""
    severity: Severity = Severity.MEDIUM
    confidence: float = 0.0
    proposed_diff: str = ""
    autonomy_decision: AutonomyDecision = AutonomyDecision.HITL_REQUIRED
    autonomy_reasons: list[str] = field(default_factory=list)
    fix_branch: str = ""
    pr_url: str = ""
    slack_thread_ts: str = ""
