# Bug Triage & Solve Agent — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python agent that ingests GitHub bug issues for the Vikunja fork, triages them (reproduce → root cause → severity), and fixes them autonomously or with Slack-based human approval.

**Architecture:** FastAPI webhook server (port 9090) receives GitHub issue events and dispatches them to an orchestrator that runs a triage sub-agent (Haiku → Opus) and a solve sub-agent (Opus), both implemented as manual Claude agentic loops with shell/file/GitHub tools. An autonomy module decides whether to auto-merge or require Slack approval.

**Tech Stack:** Python 3.11+, `anthropic` SDK (sync), `fastapi`, `uvicorn`, `requests`, `slack-sdk`, `slack-bolt`, `pytest`, `responses` (HTTP mocking)

**Spec:** `docs/superpowers/specs/2026-08-18-bug-triage-agent-design.md`

## Global Constraints

- Python 3.11+ (use `list[str]` not `List[str]`, `str | None` not `Optional[str]`)
- Anthropic SDK: sync client (`anthropic.Anthropic()`), model strings exactly as: `claude-haiku-4-5`, `claude-opus-4-8`
- GitHub API: sync `requests` library; auth via `Authorization: Bearer {GITHUB_TOKEN}` header
- Webhook server on port **9090** (Vikunja backend: 3456, frontend: 4173)
- All env vars loaded from `.env` via `python-dotenv`; never hardcode secrets
- No raw SQL; no direct DB access from agent — interact with Vikunja via its REST API only
- Vikunja repo path: configurable via `VIKUNJA_REPO_PATH` env var, default `/Users/kadishay/Code/vikunja`

---

## File Map

```
port/
├── agent/
│   ├── __init__.py
│   ├── main.py               # CLI entry: --issue N (manual) or webhook server mode
│   ├── models.py             # BugContext dataclass + Severity/AutonomyDecision enums
│   ├── orchestrator.py       # Top-level pipeline: triage → autonomy → solve
│   ├── triage.py             # Triage agentic loop (Haiku parse → Opus root cause → Haiku classify)
│   ├── solve.py              # Solve agentic loop (Opus fix → autonomy → apply → test → PR)
│   ├── autonomy.py           # Autonomy decision matrix (pure functions, fully unit-testable)
│   ├── hitl.py               # HITL interface: Phase 1 polls GitHub; Phase 2 waits on Slack
│   ├── webhook_server.py     # FastAPI app: validates GitHub webhook, enqueues events
│   ├── slack_client.py       # Phase 2: Slack Bolt app for status updates + HITL
│   └── tools/
│       ├── __init__.py
│       ├── github_tools.py   # GitHub REST API wrappers
│       ├── shell_tools.py    # run_shell, git_diff, run_tests helpers
│       └── file_tools.py     # read_file, write_file
├── bugs/
│   ├── introduce_bugs.sh     # Applies both bugs to the Vikunja fork
│   └── revert_bugs.sh        # Reverts both bugs (for demo reset)
├── tests/
│   ├── __init__.py
│   ├── test_models.py
│   ├── test_autonomy.py
│   ├── test_github_tools.py
│   ├── test_shell_tools.py
│   ├── test_triage.py
│   ├── test_solve.py
│   ├── test_hitl.py
│   └── test_webhook_server.py
├── requirements.txt
├── .env.sample
└── pytest.ini
```

---

### Task 1: Project scaffolding + BugContext model

**Files:**
- Create: `port/requirements.txt`
- Create: `port/.env.sample`
- Create: `port/pytest.ini`
- Create: `port/agent/__init__.py`
- Create: `port/agent/models.py`
- Create: `port/tests/__init__.py`
- Create: `port/tests/test_models.py`

**Interfaces:**
- Produces: `BugContext`, `Severity`, `AutonomyDecision` — imported by every other module

- [ ] **Step 1: Write requirements.txt**

```
# port/requirements.txt
anthropic>=0.40.0
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
requests>=2.32.0
python-dotenv>=1.0.0
slack-sdk>=3.30.0
slack-bolt>=1.20.0
pytest>=8.0.0
pytest-asyncio>=0.23.0
responses>=0.25.0
httpx>=0.27.0
```

- [ ] **Step 2: Write .env.sample**

```
# port/.env.sample
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=github_pat_...
GITHUB_REPO=kadishay/vikunja          # owner/repo of the Vikunja fork
GITHUB_WEBHOOK_SECRET=your-secret-here
VIKUNJA_REPO_PATH=/Users/kadishay/Code/vikunja
VIKUNJA_API_BASE=http://localhost:3456
VIKUNJA_API_TOKEN=                    # Vikunja API token for the agent user

# Phase 2
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=#bug-triage
```

- [ ] **Step 3: Write pytest.ini**

```ini
# port/pytest.ini
[pytest]
testpaths = tests
asyncio_mode = auto
```

- [ ] **Step 4: Write the failing test for BugContext**

```python
# port/tests/test_models.py
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
    assert AutonomyDecision.AUTO_MERGE == "AUTO_MERGE"
    assert AutonomyDecision.HITL_REQUIRED == "HITL_REQUIRED"
    assert AutonomyDecision.ESCALATE_ONLY == "ESCALATE_ONLY"
```

- [ ] **Step 5: Run test — expect ImportError (module doesn't exist yet)**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_models.py -v
```
Expected: `ModuleNotFoundError: No module named 'agent'`

- [ ] **Step 6: Write agent/models.py**

```python
# port/agent/models.py
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
```

Also create `port/agent/__init__.py` (empty) and `port/tests/__init__.py` (empty).

- [ ] **Step 7: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && pip install -r requirements.txt && python -m pytest tests/test_models.py -v
```
Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/ tests/ requirements.txt .env.sample pytest.ini
git commit -m "feat: scaffold agent repo with BugContext model"
```

---

### Task 2: Introduce bugs into Vikunja fork

**Files:**
- Modify: `/Users/kadishay/Code/vikunja/pkg/models/task_overdue_reminder.go` (line 41)
- Modify: `/Users/kadishay/Code/vikunja/frontend/src/stores/kanban.ts` (line 173)
- Create: `port/bugs/introduce_bugs.sh`
- Create: `port/bugs/revert_bugs.sh`

**Interfaces:**
- Produces: Two reproducible bugs with known locations and fix diffs

**Bug 1 — Backend (Go):**
File: `pkg/models/task_overdue_reminder.go`, line 41  
Original: `nextMinute.Add(time.Hour*14).Format(dbTimeFormat)`  
Buggy:    `nextMinute.Add(time.Hour*38).Format(dbTimeFormat)`  
Effect: Tasks due in the next 38 hours (including tomorrow) are erroneously flagged as overdue and trigger overdue-reminder emails/webhooks.

**Bug 2 — Frontend (Vue 3):**
File: `frontend/src/stores/kanban.ts`, line 173  
Original: `currentTaskBucket.id !== currentView.doneBucketId`  
Buggy:    `currentTaskBucket.id === currentView.doneBucketId`  
Effect: Completing a task in Kanban view doesn't move it to the Done column (the condition that triggers `moveTaskToBucket` now fires only when the task is _already_ in the done bucket — a no-op).

- [ ] **Step 1: Verify exact line content before writing scripts**

```bash
grep -n "time.Hour\*14" /Users/kadishay/Code/vikunja/pkg/models/task_overdue_reminder.go
grep -n "currentTaskBucket.id !== currentView.doneBucketId" /Users/kadishay/Code/vikunja/frontend/src/stores/kanban.ts
```
Expected: both lines match. If not, adjust the sed patterns in steps below accordingly.

- [ ] **Step 2: Write introduce_bugs.sh**

```bash
#!/usr/bin/env bash
# port/bugs/introduce_bugs.sh
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Introducing backend bug: overdue window expanded to 38h"
sed -i '' 's/nextMinute\.Add(time\.Hour\*14)/nextMinute.Add(time.Hour*38)/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Introducing frontend bug: done bucket condition inverted"
sed -i '' 's/currentTaskBucket\.id !== currentView\.doneBucketId/currentTaskBucket.id === currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Bugs introduced. Run revert_bugs.sh to undo."
```

- [ ] **Step 3: Write revert_bugs.sh**

```bash
#!/usr/bin/env bash
# port/bugs/revert_bugs.sh
set -euo pipefail

VIKUNJA="${VIKUNJA_REPO_PATH:-/Users/kadishay/Code/vikunja}"

echo "Reverting backend bug"
sed -i '' 's/nextMinute\.Add(time\.Hour\*38)/nextMinute.Add(time.Hour*14)/' \
  "$VIKUNJA/pkg/models/task_overdue_reminder.go"

echo "Reverting frontend bug"
sed -i '' 's/currentTaskBucket\.id === currentView\.doneBucketId/currentTaskBucket.id !== currentView.doneBucketId/' \
  "$VIKUNJA/frontend/src/stores/kanban.ts"

echo "Bugs reverted."
```

- [ ] **Step 4: Apply bugs and verify**

```bash
cd /Users/kadishay/Code/port && chmod +x bugs/introduce_bugs.sh bugs/revert_bugs.sh
bash bugs/introduce_bugs.sh

# Verify backend bug applied
grep "time.Hour\*38" /Users/kadishay/Code/vikunja/pkg/models/task_overdue_reminder.go

# Verify frontend bug applied  
grep "currentTaskBucket.id === currentView.doneBucketId" /Users/kadishay/Code/vikunja/frontend/src/stores/kanban.ts
```
Expected: both greps return matches.

- [ ] **Step 5: Verify revert works**

```bash
bash /Users/kadishay/Code/port/bugs/revert_bugs.sh
grep "time.Hour\*14" /Users/kadishay/Code/vikunja/pkg/models/task_overdue_reminder.go
grep "currentTaskBucket.id !== currentView.doneBucketId" /Users/kadishay/Code/vikunja/frontend/src/stores/kanban.ts
```
Expected: original lines restored.

- [ ] **Step 6: Re-apply bugs for development**

```bash
bash /Users/kadishay/Code/port/bugs/introduce_bugs.sh
```

- [ ] **Step 7: Commit scripts (not the Vikunja changes — those stay uncommitted in the fork)**

```bash
cd /Users/kadishay/Code/port && git add bugs/
git commit -m "feat: add introduce/revert bug scripts for Vikunja demo"
```

---

### Task 3: Tool layer — shell and file operations

**Files:**
- Create: `port/agent/tools/__init__.py`
- Create: `port/agent/tools/shell_tools.py`
- Create: `port/agent/tools/file_tools.py`
- Create: `port/tests/test_shell_tools.py`

**Interfaces:**
- Produces:
  - `run_shell(cmd: str, cwd: str | None = None, timeout: int = 120) -> tuple[str, str, int]` — returns (stdout, stderr, returncode)
  - `git_diff(repo_path: str) -> str`
  - `read_file(path: str) -> str`
  - `write_file(path: str, content: str) -> None`

- [ ] **Step 1: Write failing tests**

```python
# port/tests/test_shell_tools.py
import os
from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file

def test_run_shell_success():
    stdout, stderr, rc = run_shell("echo hello")
    assert rc == 0
    assert stdout.strip() == "hello"
    assert stderr == ""

def test_run_shell_failure():
    stdout, stderr, rc = run_shell("ls /nonexistent-path-xyz")
    assert rc != 0

def test_run_shell_cwd(tmp_path):
    stdout, _, rc = run_shell("pwd", cwd=str(tmp_path))
    assert rc == 0
    assert str(tmp_path) in stdout

def test_read_write_file(tmp_path):
    path = str(tmp_path / "test.txt")
    write_file(path, "hello world")
    assert read_file(path) == "hello world"

def test_git_diff_empty_on_clean_repo(tmp_path):
    run_shell("git init", cwd=str(tmp_path))
    run_shell("git commit --allow-empty -m 'init'", cwd=str(tmp_path))
    diff = git_diff(str(tmp_path))
    assert diff == ""
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_shell_tools.py -v
```

- [ ] **Step 3: Write shell_tools.py**

```python
# port/agent/tools/shell_tools.py
import subprocess

def run_shell(cmd: str, cwd: str | None = None, timeout: int = 120) -> tuple[str, str, int]:
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
    )
    return result.stdout, result.stderr, result.returncode

def git_diff(repo_path: str) -> str:
    stdout, _, _ = run_shell("git diff", cwd=repo_path)
    return stdout

def git_diff_staged(repo_path: str) -> str:
    stdout, _, _ = run_shell("git diff --staged", cwd=repo_path)
    return stdout
```

- [ ] **Step 4: Write file_tools.py**

```python
# port/agent/tools/file_tools.py
def read_file(path: str) -> str:
    with open(path) as f:
        return f.read()

def write_file(path: str, content: str) -> None:
    with open(path, "w") as f:
        f.write(content)
```

Also create `port/agent/tools/__init__.py` (empty).

- [ ] **Step 5: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_shell_tools.py -v
```
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/tools/ tests/test_shell_tools.py
git commit -m "feat: add shell and file tool implementations"
```

---

### Task 4: GitHub API client

**Files:**
- Create: `port/agent/tools/github_tools.py`
- Create: `port/tests/test_github_tools.py`

**Interfaces:**
- Consumes: `GITHUB_TOKEN`, `GITHUB_REPO` env vars
- Produces: `GitHubClient` with methods:
  - `get_issue(issue_number: int) -> dict`
  - `post_comment(issue_number: int, body: str) -> dict`
  - `get_comments(issue_number: int) -> list[dict]`
  - `add_label(issue_number: int, label: str) -> None`
  - `create_pr(title: str, body: str, head: str, base: str = "main") -> dict`

- [ ] **Step 1: Write failing tests**

```python
# port/tests/test_github_tools.py
import responses as resp_mock
import requests
import pytest
from agent.tools.github_tools import GitHubClient

REPO = "test-owner/test-repo"

@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", REPO)

@resp_mock.activate
def test_get_issue():
    resp_mock.add(
        resp_mock.GET,
        f"https://api.github.com/repos/{REPO}/issues/42",
        json={"number": 42, "title": "Bug: tasks overdue"},
        status=200,
    )
    client = GitHubClient()
    issue = client.get_issue(42)
    assert issue["number"] == 42

@resp_mock.activate
def test_post_comment():
    resp_mock.add(
        resp_mock.POST,
        f"https://api.github.com/repos/{REPO}/issues/42/comments",
        json={"id": 1, "body": "triage done"},
        status=201,
    )
    client = GitHubClient()
    result = client.post_comment(42, "triage done")
    assert result["id"] == 1

@resp_mock.activate
def test_get_comments_empty():
    resp_mock.add(
        resp_mock.GET,
        f"https://api.github.com/repos/{REPO}/issues/42/comments",
        json=[],
        status=200,
    )
    client = GitHubClient()
    comments = client.get_comments(42)
    assert comments == []

@resp_mock.activate
def test_create_pr():
    resp_mock.add(
        resp_mock.POST,
        f"https://api.github.com/repos/{REPO}/pulls",
        json={"number": 7, "html_url": "https://github.com/test-owner/test-repo/pull/7"},
        status=201,
    )
    client = GitHubClient()
    pr = client.create_pr("fix: overdue window", "fixes #42", "fix/issue-42")
    assert pr["number"] == 7
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_github_tools.py -v
```

- [ ] **Step 3: Write github_tools.py**

```python
# port/agent/tools/github_tools.py
import os
import requests

GITHUB_API = "https://api.github.com"

class GitHubClient:
    def __init__(self):
        self._headers = {
            "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._repo = os.environ["GITHUB_REPO"]

    def _url(self, path: str) -> str:
        return f"{GITHUB_API}/repos/{self._repo}/{path}"

    def get_issue(self, issue_number: int) -> dict:
        r = requests.get(self._url(f"issues/{issue_number}"), headers=self._headers)
        r.raise_for_status()
        return r.json()

    def post_comment(self, issue_number: int, body: str) -> dict:
        r = requests.post(
            self._url(f"issues/{issue_number}/comments"),
            headers=self._headers,
            json={"body": body},
        )
        r.raise_for_status()
        return r.json()

    def get_comments(self, issue_number: int) -> list[dict]:
        r = requests.get(self._url(f"issues/{issue_number}/comments"), headers=self._headers)
        r.raise_for_status()
        return r.json()

    def add_label(self, issue_number: int, label: str) -> None:
        r = requests.post(
            self._url(f"issues/{issue_number}/labels"),
            headers=self._headers,
            json={"labels": [label]},
        )
        r.raise_for_status()

    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> dict:
        r = requests.post(
            self._url("pulls"),
            headers=self._headers,
            json={"title": title, "body": body, "head": head, "base": base},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 4: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_github_tools.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/tools/github_tools.py tests/test_github_tools.py
git commit -m "feat: add GitHub API client"
```

---

### Task 5: Autonomy decision module

**Files:**
- Create: `port/agent/autonomy.py`
- Create: `port/tests/test_autonomy.py`

**Interfaces:**
- Consumes: `BugContext`, `Severity`, `AutonomyDecision` from `agent.models`
- Produces: `evaluate_autonomy(ctx: BugContext, diff: str) -> tuple[AutonomyDecision, list[str]]`

- [ ] **Step 1: Write the failing tests**

```python
# port/tests/test_autonomy.py
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

LARGE_DIFF = SMALL_DIFF * 20  # >30 lines changed

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
    auth_diff = SMALL_DIFF.replace("task_overdue_reminder", "auth_middleware")
    auth_diff = auth_diff + "\n+import auth\n"
    diff_with_auth = SMALL_DIFF + "\n+// auth token handling\n"
    decision, reasons = evaluate_autonomy(_ctx(), diff_with_auth)
    assert decision == AutonomyDecision.ESCALATE_ONLY
    assert any("auth" in r for r in reasons)
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_autonomy.py -v
```

- [ ] **Step 3: Write autonomy.py**

```python
# port/agent/autonomy.py
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
```

- [ ] **Step 4: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_autonomy.py -v
```
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/autonomy.py tests/test_autonomy.py
git commit -m "feat: add autonomy decision module"
```

---

### Task 6: Triage agent (agentic loop)

**Files:**
- Create: `port/agent/triage.py`
- Create: `port/tests/test_triage.py`

**Interfaces:**
- Consumes: `BugContext`, `Severity`; `GitHubClient`; `run_shell`, `read_file`; `anthropic.Anthropic()`
- Produces: `run_triage(ctx: BugContext, gh: GitHubClient) -> BugContext` — mutates and returns ctx with `reproduction_log`, `root_cause`, `confidence`, `severity` populated; posts a triage comment to the GitHub issue

The triage agent runs three Claude calls:
1. **Haiku** — parse issue body into structured reproduction commands
2. **Haiku (tool runner loop)** — run reproduction commands, collect output
3. **Opus** — analyze reproduction log + source files → root cause + confidence (structured output)
4. **Haiku** — classify severity from root cause (structured output)

- [ ] **Step 1: Write failing tests**

```python
# port/tests/test_triage.py
from unittest.mock import patch, MagicMock
from agent.models import BugContext, Severity

def _end_turn(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.content = [block]
    return r

@patch("agent.triage.GitHubClient")
@patch("agent.triage.client")
def test_triage_sets_severity_high(mock_client, mock_gh_class, tmp_path):
    from agent.triage import run_triage

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh

    # Call 1: Haiku parses issue → reproduction commands
    mock_client.messages.create.side_effect = [
        _make_end_turn_response("Run: curl http://localhost:3456/api/v1/tasks"),
        # Call 2: Haiku reproduction loop ends immediately
        _make_end_turn_response("Reproduction complete. Output: tasks due tomorrow flagged overdue."),
        # Call 3: Opus root cause
        _make_end_turn_response('{"root_cause": "time.Hour*38 window too large", "confidence": 0.92}'),
        # Call 4: Haiku severity
        _make_end_turn_response('{"severity": "HIGH"}'),
    ]

    ctx = BugContext(
        issue_number=42,
        issue_title="Tasks due tomorrow shown as overdue",
        issue_body="## Steps\n1. Create task due tomorrow\n2. Check overdue list",
        repo_path=str(tmp_path),
    )

    result = run_triage(ctx, mock_gh)

    assert result.severity == Severity.HIGH
    assert result.confidence == 0.92
    assert "time.Hour*38" in result.root_cause
    assert result.reproduction_log != ""
    mock_gh.post_comment.assert_called_once()
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_triage.py -v
```

- [ ] **Step 3: Write triage.py**

```python
# port/agent/triage.py
import json
import os
import anthropic
from agent.models import BugContext, Severity
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell
from agent.tools.file_tools import read_file

client = anthropic.Anthropic()

_TRIAGE_TOOLS = [
    {
        "name": "run_shell",
        "description": "Run a shell command in the Vikunja repo or against the Vikunja API.",
        "input_schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer", "default": 60}},
            "required": ["cmd"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a source file from the Vikunja repo.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

def run_triage(ctx: BugContext, gh: GitHubClient) -> BugContext:
    steps = _parse_reproduction_steps(ctx)
    ctx.reproduction_log = _reproduce(ctx, steps)
    ctx.root_cause, ctx.confidence = _analyze_root_cause(ctx)
    ctx.severity = _classify_severity(ctx)
    _post_triage_comment(ctx, gh)
    return ctx

def _parse_reproduction_steps(ctx: BugContext) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the exact shell/curl commands needed to reproduce this bug.\n\n"
                f"Issue: {ctx.issue_title}\n\n{ctx.issue_body}\n\n"
                "List only the commands, one per line. Assume Vikunja API is at "
                f"{os.environ.get('VIKUNJA_API_BASE', 'http://localhost:3456')}."
            ),
        }],
    )
    return next(b.text for b in response.content if b.type == "text")

def _reproduce(ctx: BugContext, steps: str) -> str:
    messages = [{
        "role": "user",
        "content": (
            f"Reproduce this bug by running these steps against the Vikunja instance.\n\n"
            f"Steps:\n{steps}\n\n"
            f"Vikunja repo: {ctx.repo_path}\n"
            "Capture all output. When done, summarize what you observed."
        ),
    }]
    log_parts: list[str] = []

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=_TRIAGE_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            log_parts.extend(b.text for b in response.content if b.type == "text")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, ctx.repo_path)
                log_parts.append(f"[{block.name}] {block.input}\n{result}")
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    return "\n".join(log_parts)

def _analyze_root_cause(ctx: BugContext) -> tuple[str, float]:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{
            "role": "user",
            "content": (
                f"Analyze this Vikunja bug. Identify the root cause and your confidence (0.0–1.0).\n\n"
                f"Issue: {ctx.issue_title}\n\n"
                f"Reproduction log:\n{ctx.reproduction_log}\n\n"
                "Respond with JSON only: {\"root_cause\": \"...\", \"confidence\": 0.XX}"
            ),
        }],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text.strip().strip("```json").strip("```").strip())
    return data["root_cause"], float(data["confidence"])

def _classify_severity(ctx: BugContext) -> Severity:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Classify the severity of this bug. Root cause: {ctx.root_cause}\n\n"
                "Rubric:\n"
                "- CRITICAL: data loss, auth bypass, crash on startup\n"
                "- HIGH: wrong data shown, core feature broken\n"
                "- MEDIUM: degraded UX, edge case error\n"
                "- LOW: visual glitch, non-blocking\n\n"
                'Respond with JSON only: {"severity": "HIGH"}'
            ),
        }],
    )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text.strip().strip("```json").strip("```").strip())
    return Severity(data["severity"])

def _post_triage_comment(ctx: BugContext, gh: GitHubClient) -> None:
    severity_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
    body = (
        f"## 🤖 Automated Triage Report\n\n"
        f"**Severity:** {severity_emoji.get(ctx.severity, '⚪')} {ctx.severity}\n"
        f"**Confidence:** {ctx.confidence:.0%}\n\n"
        f"### Root Cause\n{ctx.root_cause}\n\n"
        f"### Reproduction Log\n```\n{ctx.reproduction_log[:2000]}\n```\n\n"
        f"*Triage powered by Claude {('Opus' if ctx.severity in ('CRITICAL', 'HIGH') else 'Haiku')}*"
    )
    gh.post_comment(ctx.issue_number, body)

def _execute_tool(name: str, inputs: dict, repo_path: str) -> str:
    if name == "run_shell":
        stdout, stderr, rc = run_shell(inputs["cmd"], cwd=repo_path, timeout=inputs.get("timeout", 60))
        return f"[exit {rc}]\nstdout: {stdout}\nstderr: {stderr}"
    if name == "read_file":
        try:
            return read_file(inputs["path"])
        except FileNotFoundError:
            return f"File not found: {inputs['path']}"
    return f"Unknown tool: {name}"
```

- [ ] **Step 4: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_triage.py -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/triage.py tests/test_triage.py
git commit -m "feat: add triage agent (Haiku reproduce → Opus root cause → Haiku classify)"
```

---

### Task 7: HITL module (Phase 1 — GitHub comment polling)

**Files:**
- Create: `port/agent/hitl.py`
- Create: `port/tests/test_hitl.py`

**Interfaces:**
- Produces:
  - `wait_for_approval(ctx: BugContext, gh: GitHubClient, timeout_seconds: int = 1800) -> bool`
    — Returns `True` if human replied `/approve`, `False` if `/reject` or timeout
  - `HITLTimeout` exception

- [ ] **Step 1: Write failing tests**

```python
# port/tests/test_hitl.py
import time
import pytest
from unittest.mock import patch, MagicMock
from agent.hitl import wait_for_approval, HITLTimeout
from agent.models import BugContext

def _ctx():
    return BugContext(issue_number=42, issue_title="Bug", issue_body="...", repo_path="/tmp")

def _mock_gh(comments: list[dict]):
    gh = MagicMock()
    gh.get_comments.return_value = comments
    return gh

def test_returns_true_on_approve():
    gh = _mock_gh([{"body": "/approve", "created_at": "2026-01-01T00:01:00Z"}])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)
    assert result is True

def test_returns_false_on_reject():
    gh = _mock_gh([{"body": "/reject", "created_at": "2026-01-01T00:01:00Z"}])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)
    assert result is False

def test_raises_on_timeout():
    gh = _mock_gh([])  # no approval ever arrives
    with patch("agent.hitl.time.sleep"), patch("agent.hitl.time.time", side_effect=[0, 0, 999]):
        with pytest.raises(HITLTimeout):
            wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)

def test_ignores_comments_without_command():
    gh = _mock_gh([
        {"body": "Looks like a real bug", "created_at": "2026-01-01T00:01:00Z"},
        {"body": "/approve", "created_at": "2026-01-01T00:02:00Z"},
    ])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=30, poll_interval=1)
    assert result is True
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_hitl.py -v
```

- [ ] **Step 3: Write hitl.py**

```python
# port/agent/hitl.py
import time
from agent.models import BugContext
from agent.tools.github_tools import GitHubClient

class HITLTimeout(Exception):
    pass

def wait_for_approval(
    ctx: BugContext,
    gh: GitHubClient,
    timeout_seconds: int = 1800,
    poll_interval: int = 60,
) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        comments = gh.get_comments(ctx.issue_number)
        for comment in comments:
            body = comment.get("body", "").strip().lower()
            if body.startswith("/approve"):
                return True
            if body.startswith("/reject"):
                return False
        time.sleep(poll_interval)
    raise HITLTimeout(f"No response received within {timeout_seconds}s for issue #{ctx.issue_number}")
```

- [ ] **Step 4: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_hitl.py -v
```
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/hitl.py tests/test_hitl.py
git commit -m "feat: add HITL module (GitHub comment polling for /approve or /reject)"
```

---

### Task 8: Solve agent (agentic loop)

**Files:**
- Create: `port/agent/solve.py`
- Create: `port/tests/test_solve.py`

**Interfaces:**
- Consumes: `BugContext`, `AutonomyDecision`; `GitHubClient`; `evaluate_autonomy`; `wait_for_approval`; `run_shell`, `write_file`, `git_diff`; `anthropic.Anthropic()`
- Produces: `run_solve(ctx: BugContext, gh: GitHubClient) -> BugContext` — mutates ctx with `proposed_diff`, `autonomy_decision`, `fix_branch`, `pr_url`; applies fix and opens PR if approved

- [ ] **Step 1: Write failing tests**

```python
# port/tests/test_solve.py
from unittest.mock import patch, MagicMock, call
import pytest
from agent.models import BugContext, Severity, AutonomyDecision

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

@patch("agent.solve.evaluate_autonomy", return_value=(AutonomyDecision.AUTO_MERGE, ["all criteria met"]))
@patch("agent.solve.git_diff", return_value="diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
@patch("agent.solve.run_shell", return_value=("ok", "", 0))
@patch("agent.solve.client")
def test_auto_merge_creates_pr(mock_client, mock_shell, mock_diff, mock_autonomy):
    from agent.solve import run_solve
    mock_client.messages.create.return_value = _end_turn(
        'Apply fix: change time.Hour*38 to time.Hour*14 in task_overdue_reminder.go'
    )
    mock_gh = MagicMock()
    ctx = _ctx()
    result = run_solve(ctx, mock_gh)
    assert result.autonomy_decision == AutonomyDecision.AUTO_MERGE
    mock_gh.create_pr.assert_called_once()

@patch("agent.solve.evaluate_autonomy", return_value=(AutonomyDecision.HITL_REQUIRED, ["CRITICAL severity"]))
@patch("agent.solve.wait_for_approval", return_value=False)
@patch("agent.solve.git_diff", return_value="diff --git a/f b/f\n+++ b/f\n@@ -1 +1 @@\n-old\n+new\n")
@patch("agent.solve.run_shell", return_value=("ok", "", 0))
@patch("agent.solve.client")
def test_hitl_rejected_skips_pr(mock_client, mock_shell, mock_diff, mock_approval, mock_autonomy):
    from agent.solve import run_solve
    mock_client.messages.create.return_value = _end_turn("Apply this fix.")
    mock_gh = MagicMock()
    ctx = _ctx(severity=Severity.CRITICAL)
    result = run_solve(ctx, mock_gh)
    mock_gh.create_pr.assert_not_called()
    mock_gh.post_comment.assert_called()  # posted rejection notice
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_solve.py -v
```

- [ ] **Step 3: Write solve.py**

```python
# port/agent/solve.py
import json
import os
import anthropic
from agent.models import BugContext, AutonomyDecision
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file
from agent.autonomy import evaluate_autonomy
from agent.hitl import wait_for_approval, HITLTimeout

client = anthropic.Anthropic()

_SOLVE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a source file from the Vikunja repo.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (overwrite) a file in the Vikunja repo with the fixed content.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command (build, test, git operations).",
        "input_schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}, "timeout": {"type": "integer", "default": 120}},
            "required": ["cmd"],
        },
    },
]

def run_solve(ctx: BugContext, gh: GitHubClient) -> BugContext:
    fix_branch = f"fix/issue-{ctx.issue_number}"
    _create_fix_branch(ctx.repo_path, fix_branch)
    ctx.fix_branch = fix_branch

    _apply_fix(ctx)

    diff = git_diff(ctx.repo_path)
    ctx.proposed_diff = diff

    decision, reasons = evaluate_autonomy(ctx, diff)
    ctx.autonomy_decision = decision
    ctx.autonomy_reasons = reasons

    if decision == AutonomyDecision.ESCALATE_ONLY:
        gh.post_comment(ctx.issue_number, _escalate_comment(ctx))
        return ctx

    if decision == AutonomyDecision.AUTO_MERGE:
        return _finish_and_open_pr(ctx, gh, diff)

    # HITL required
    gh.post_comment(ctx.issue_number, _hitl_comment(ctx, diff, reasons))
    try:
        approved = wait_for_approval(ctx, gh)
    except HITLTimeout:
        gh.post_comment(ctx.issue_number, "⏰ No response received within 30 minutes. Auto-fix aborted.")
        _abort_fix(ctx.repo_path, fix_branch)
        return ctx

    if not approved:
        gh.post_comment(ctx.issue_number, "❌ Fix rejected by human reviewer. Branch deleted.")
        _abort_fix(ctx.repo_path, fix_branch)
        return ctx

    return _finish_and_open_pr(ctx, gh, diff)

def _apply_fix(ctx: BugContext) -> None:
    messages = [{
        "role": "user",
        "content": (
            f"Fix this Vikunja bug. Read the relevant files and apply the minimal change.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause: {ctx.root_cause}\n"
            f"Repo: {ctx.repo_path}\n\n"
            "After applying the fix, run the relevant tests to verify it works."
        ),
    }]

    while True:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=8192,
            thinking={"type": "adaptive"},
            tools=_SOLVE_TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, ctx.repo_path)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

def _finish_and_open_pr(ctx: BugContext, gh: GitHubClient, diff: str) -> BugContext:
    run_shell(f"git -C {ctx.repo_path} add -A && git -C {ctx.repo_path} commit -m 'fix: resolve #{ctx.issue_number} - {ctx.issue_title[:60]}'")
    run_shell(f"git -C {ctx.repo_path} push origin {ctx.fix_branch}")

    pr = gh.create_pr(
        title=f"fix: resolve #{ctx.issue_number} — {ctx.issue_title[:60]}",
        body=_pr_body(ctx, diff),
        head=ctx.fix_branch,
    )
    ctx.pr_url = pr.get("html_url", "")
    gh.post_comment(ctx.issue_number, f"✅ Fix applied automatically. PR: {ctx.pr_url}")
    return ctx

def _create_fix_branch(repo_path: str, branch: str) -> None:
    run_shell(f"git -C {repo_path} checkout -b {branch}")

def _abort_fix(repo_path: str, branch: str) -> None:
    run_shell(f"git -C {repo_path} checkout main && git -C {repo_path} branch -D {branch}")

def _hitl_comment(ctx: BugContext, diff: str, reasons: list[str]) -> str:
    return (
        f"## 🔧 Proposed Fix for #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**HITL required because:** {'; '.join(reasons)}\n\n"
        f"```diff\n{diff[:3000]}\n```\n\n"
        f"Reply `/approve` to merge or `/reject` to abort. Timeout: 30 minutes."
    )

def _escalate_comment(ctx: BugContext) -> str:
    return (
        f"## ⚠️ Escalated to human — #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        "This change touches auth, migrations, or security-sensitive code. "
        "Automatic fix has been skipped. Please review and fix manually."
    )

def _pr_body(ctx: BugContext, diff: str) -> str:
    auto = ctx.autonomy_decision == AutonomyDecision.AUTO_MERGE
    return (
        f"Fixes #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**Confidence:** {ctx.confidence:.0%}\n\n"
        f"**Merge decision:** {'Automatic (all autonomy criteria met)' if auto else 'Human approved via GitHub'}\n\n"
        f"*Fix proposed by Claude Opus 4.8*"
    )

def _execute_tool(name: str, inputs: dict, repo_path: str) -> str:
    if name == "read_file":
        try:
            return read_file(inputs["path"])
        except FileNotFoundError:
            return f"File not found: {inputs['path']}"
    if name == "write_file":
        write_file(inputs["path"], inputs["content"])
        return f"Written: {inputs['path']}"
    if name == "run_shell":
        stdout, stderr, rc = run_shell(inputs["cmd"], cwd=repo_path, timeout=inputs.get("timeout", 120))
        return f"[exit {rc}]\n{stdout}\n{stderr}"
    return f"Unknown tool: {name}"
```

- [ ] **Step 4: Run tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_solve.py -v
```
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/solve.py tests/test_solve.py
git commit -m "feat: add solve agent (Opus fix → autonomy check → auto-merge or HITL)"
```

---

### Task 9: Webhook server + orchestrator + CLI entry

**Files:**
- Create: `port/agent/orchestrator.py`
- Create: `port/agent/webhook_server.py`
- Create: `port/agent/main.py`
- Create: `port/tests/test_webhook_server.py`

**Interfaces:**
- Consumes: `run_triage`, `run_solve`, `GitHubClient`, `BugContext`
- Produces: 
  - `run_pipeline(issue_number: int) -> BugContext` — full triage + solve for one issue
  - FastAPI app on port 9090 that receives GitHub webhooks
  - `main.py` CLI: `python -m agent.main --issue 42` or `python -m agent.main --serve`

- [ ] **Step 1: Write failing tests for webhook server**

```python
# port/tests/test_webhook_server.py
import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient

@pytest.fixture()
def app_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("VIKUNJA_REPO_PATH", "/tmp")

@pytest.fixture()
def client(app_env):
    from agent.webhook_server import app
    return TestClient(app)

def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

def test_valid_webhook_returns_200(client, monkeypatch):
    monkeypatch.setattr("agent.webhook_server.enqueue_issue", lambda n: None)
    payload = json.dumps({"action": "opened", "issue": {"number": 42}}).encode()
    sig = _sign(payload, "test-secret")
    r = client.post("/webhook", content=payload, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"})
    assert r.status_code == 202

def test_invalid_signature_returns_403(client):
    payload = json.dumps({"action": "opened", "issue": {"number": 42}}).encode()
    r = client.post("/webhook", content=payload, headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues"})
    assert r.status_code == 403

def test_non_opened_action_returns_200_no_enqueue(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("agent.webhook_server.enqueue_issue", lambda n: enqueued.append(n))
    payload = json.dumps({"action": "edited", "issue": {"number": 42}}).encode()
    sig = _sign(payload, "test-secret")
    r = client.post("/webhook", content=payload, headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"})
    assert r.status_code == 200
    assert enqueued == []
```

- [ ] **Step 2: Run — expect ImportError**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_webhook_server.py -v
```

- [ ] **Step 3: Write orchestrator.py**

```python
# port/agent/orchestrator.py
import os
from dotenv import load_dotenv
load_dotenv()

from agent.models import BugContext
from agent.tools.github_tools import GitHubClient
from agent.triage import run_triage
from agent.solve import run_solve

def run_pipeline(issue_number: int) -> BugContext:
    gh = GitHubClient()
    issue = gh.get_issue(issue_number)

    ctx = BugContext(
        issue_number=issue_number,
        issue_title=issue["title"],
        issue_body=issue.get("body") or "",
        repo_path=os.environ.get("VIKUNJA_REPO_PATH", "/Users/kadishay/Code/vikunja"),
    )

    print(f"[orchestrator] Starting triage for issue #{issue_number}: {issue['title']}")
    ctx = run_triage(ctx, gh)
    print(f"[orchestrator] Triage complete: {ctx.severity} | confidence {ctx.confidence:.0%}")

    from agent.models import Severity
    if ctx.severity in (Severity.CRITICAL, Severity.HIGH):
        print(f"[orchestrator] Starting solve for #{issue_number}")
        ctx = run_solve(ctx, gh)
        print(f"[orchestrator] Solve complete: {ctx.autonomy_decision} | PR: {ctx.pr_url or 'none'}")
    else:
        gh.add_label(issue_number, f"severity:{ctx.severity.lower()}")
        print(f"[orchestrator] {ctx.severity} severity — labelled, no auto-fix attempted")

    return ctx
```

- [ ] **Step 4: Write webhook_server.py**

```python
# port/agent/webhook_server.py
import hashlib
import hmac
import json
import os
import threading
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Response

app = FastAPI()

def enqueue_issue(issue_number: int) -> None:
    from agent.orchestrator import run_pipeline
    thread = threading.Thread(target=run_pipeline, args=(issue_number,), daemon=True)
    thread.start()

@app.post("/webhook")
async def github_webhook(request: Request) -> Response:
    body = await request.body()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    sig_header = request.headers.get("X-Hub-Signature-256", "")

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        return Response(content="Invalid signature", status_code=403)

    event = request.headers.get("X-GitHub-Event", "")
    if event != "issues":
        return Response(content="Ignored", status_code=200)

    payload = json.loads(body)
    if payload.get("action") != "opened":
        return Response(content="Ignored (not opened)", status_code=200)

    issue_number = payload["issue"]["number"]
    print(f"[webhook] Received issue #{issue_number} — dispatching pipeline")
    enqueue_issue(issue_number)
    return Response(content="Accepted", status_code=202)
```

- [ ] **Step 5: Write main.py**

```python
# port/agent/main.py
import argparse
import uvicorn

def main():
    parser = argparse.ArgumentParser(description="Bug Triage Agent")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--issue", type=int, help="Run pipeline for a specific GitHub issue number")
    group.add_argument("--serve", action="store_true", help="Start the webhook server on port 9090")
    args = parser.parse_args()

    if args.issue:
        from agent.orchestrator import run_pipeline
        ctx = run_pipeline(args.issue)
        print(f"\n=== Pipeline complete ===")
        print(f"Severity: {ctx.severity} | Confidence: {ctx.confidence:.0%}")
        print(f"Decision: {ctx.autonomy_decision}")
        print(f"PR: {ctx.pr_url or 'none'}")
    else:
        print("Starting webhook server on port 9090...")
        uvicorn.run("agent.webhook_server:app", host="0.0.0.0", port=9090, reload=False)

if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run webhook tests — expect green**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/test_webhook_server.py -v
```
Expected: 3 passed

- [ ] **Step 7: Smoke test — manual invocation help**

```bash
cd /Users/kadishay/Code/port && python -m agent.main --help
```
Expected: shows `--issue` and `--serve` options without error.

- [ ] **Step 8: Run all tests**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/ -v
```
Expected: all green

- [ ] **Step 9: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/orchestrator.py agent/webhook_server.py agent/main.py tests/test_webhook_server.py
git commit -m "feat: add orchestrator, webhook server, and CLI entry point"
```

---

### Task 10: Phase 2 — Slack integration

**Files:**
- Create: `port/agent/slack_client.py`
- Modify: `port/agent/hitl.py` — add Slack approval path
- Modify: `port/agent/orchestrator.py` — add status update calls

**Interfaces:**
- Consumes: `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` env vars
- Produces:
  - `SlackClient.post_status(message: str) -> str` — posts to channel, returns thread_ts
  - `SlackClient.post_to_thread(thread_ts: str, message: str) -> None`
  - `SlackClient.wait_for_approval(thread_ts: str, timeout: int) -> bool`
  - Updated `wait_for_approval` in hitl.py: if `SLACK_APP_TOKEN` is set, delegates to Slack

- [ ] **Step 1: Write slack_client.py**

```python
# port/agent/slack_client.py
import os
import time
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

class SlackClient:
    def __init__(self):
        self._web = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self._channel = os.environ.get("SLACK_CHANNEL", "#bug-triage")
        self._approvals: dict[str, bool | None] = {}

    def post_status(self, message: str) -> str:
        result = self._web.chat_postMessage(channel=self._channel, text=message)
        return result["ts"]

    def post_to_thread(self, thread_ts: str, message: str) -> None:
        self._web.chat_postMessage(channel=self._channel, thread_ts=thread_ts, text=message)

    def wait_for_approval(self, thread_ts: str, timeout: int = 1800) -> bool:
        app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not app_token:
            raise RuntimeError("SLACK_APP_TOKEN required for Slack HITL")

        self._approvals[thread_ts] = None
        socket = SocketModeClient(app_token=app_token, web_client=self._web)

        def handle(client: SocketModeClient, req: SocketModeRequest):
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type != "events_api":
                return
            event = req.payload.get("event", {})
            if event.get("type") != "message" or event.get("thread_ts") != thread_ts:
                return
            text = event.get("text", "").strip().lower()
            if text.startswith("/approve"):
                self._approvals[thread_ts] = True
            elif text.startswith("/reject"):
                self._approvals[thread_ts] = False

        socket.socket_mode_request_listeners.append(handle)
        socket.connect()

        deadline = time.time() + timeout
        while time.time() < deadline:
            decision = self._approvals.get(thread_ts)
            if decision is not None:
                socket.close()
                return decision
            time.sleep(5)

        socket.close()
        from agent.hitl import HITLTimeout
        raise HITLTimeout(f"No Slack response in {timeout}s")
```

- [ ] **Step 2: Update hitl.py to support Slack mode**

```python
# port/agent/hitl.py  (replace file)
import os
import time
from agent.models import BugContext
from agent.tools.github_tools import GitHubClient

class HITLTimeout(Exception):
    pass

def wait_for_approval(
    ctx: BugContext,
    gh: GitHubClient,
    timeout_seconds: int = 1800,
    poll_interval: int = 60,
) -> bool:
    if os.environ.get("SLACK_APP_TOKEN") and ctx.slack_thread_ts:
        from agent.slack_client import SlackClient
        return SlackClient().wait_for_approval(ctx.slack_thread_ts, timeout=timeout_seconds)

    # Phase 1: poll GitHub comments
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        comments = gh.get_comments(ctx.issue_number)
        for comment in comments:
            body = comment.get("body", "").strip().lower()
            if body.startswith("/approve"):
                return True
            if body.startswith("/reject"):
                return False
        time.sleep(poll_interval)
    raise HITLTimeout(f"No response received within {timeout_seconds}s for issue #{ctx.issue_number}")
```

- [ ] **Step 3: Update orchestrator.py to post Slack status updates**

Add a `_notify` helper and call it at each pipeline stage. Replace the `print` calls in `run_pipeline`:

```python
# port/agent/orchestrator.py  (updated run_pipeline)
import os
from dotenv import load_dotenv
load_dotenv()

from agent.models import BugContext, Severity
from agent.tools.github_tools import GitHubClient
from agent.triage import run_triage
from agent.solve import run_solve

def run_pipeline(issue_number: int) -> BugContext:
    gh = GitHubClient()
    issue = gh.get_issue(issue_number)

    ctx = BugContext(
        issue_number=issue_number,
        issue_title=issue["title"],
        issue_body=issue.get("body") or "",
        repo_path=os.environ.get("VIKUNJA_REPO_PATH", "/Users/kadishay/Code/vikunja"),
    )

    thread_ts = _notify(f"📥 Issue #{issue_number} received: *{issue['title']}* — starting triage")
    ctx.slack_thread_ts = thread_ts or ""

    ctx = run_triage(ctx, gh)
    _notify_thread(ctx, f"🔬 Triage complete — Severity: *{ctx.severity}* | Confidence: {ctx.confidence:.0%}\nRoot cause: {ctx.root_cause[:200]}")

    if ctx.severity in (Severity.CRITICAL, Severity.HIGH):
        _notify_thread(ctx, f"🔧 Starting automated fix for #{issue_number}...")
        ctx = run_solve(ctx, gh)
        if ctx.pr_url:
            _notify_thread(ctx, f"✅ PR opened: {ctx.pr_url}")
        else:
            _notify_thread(ctx, f"⚠️ Fix aborted or rejected. Decision: {ctx.autonomy_decision}")
    else:
        gh.add_label(issue_number, f"severity:{ctx.severity.lower()}")
        _notify_thread(ctx, f"🏷️ {ctx.severity} severity — labelled, no auto-fix")

    return ctx

def _notify(message: str) -> str | None:
    if not os.environ.get("SLACK_BOT_TOKEN"):
        print(f"[status] {message}")
        return None
    from agent.slack_client import SlackClient
    return SlackClient().post_status(message)

def _notify_thread(ctx: BugContext, message: str) -> None:
    if not os.environ.get("SLACK_BOT_TOKEN"):
        print(f"[status] {message}")
        return
    if ctx.slack_thread_ts:
        from agent.slack_client import SlackClient
        SlackClient().post_to_thread(ctx.slack_thread_ts, message)
    else:
        _notify(message)
```

- [ ] **Step 4: Verify existing tests still pass**

```bash
cd /Users/kadishay/Code/port && python -m pytest tests/ -v
```
Expected: all tests still pass (Slack code paths are only triggered when `SLACK_BOT_TOKEN` is set)

- [ ] **Step 5: Commit**

```bash
cd /Users/kadishay/Code/port && git add agent/slack_client.py agent/hitl.py agent/orchestrator.py
git commit -m "feat: add Phase 2 Slack integration for status updates and HITL"
```

---

## End-to-End Demo Flow

After completing all tasks, run the full pipeline against the real bugs:

```bash
# 1. Apply bugs
bash /Users/kadishay/Code/port/bugs/introduce_bugs.sh

# 2. Copy .env.sample and fill in real values
cp /Users/kadishay/Code/port/.env.sample /Users/kadishay/Code/port/.env
# edit .env with real ANTHROPIC_API_KEY, GITHUB_TOKEN, etc.

# 3. Open ngrok tunnel
ngrok http 9090

# 4. Configure GitHub webhook in the Vikunja fork repo:
#    Settings → Webhooks → Add webhook
#    URL: https://<ngrok-id>.ngrok.io/webhook
#    Content-type: application/json
#    Secret: matches GITHUB_WEBHOOK_SECRET in .env
#    Events: Issues

# 5. Start the agent server
cd /Users/kadishay/Code/port && python -m agent.main --serve

# 6. Open a GitHub issue in the Vikunja fork with this template:
#    Title: "Bug: tasks due in the next 38h incorrectly flagged as overdue"
#    Body:
#    ## Steps to reproduce
#    ```bash
#    # Create task due tomorrow
#    curl -X POST http://localhost:3456/api/v1/tasks \
#      -H "Authorization: Bearer $VIKUNJA_API_TOKEN" \
#      -d '{"title":"Tomorrow task","due_date":"<tomorrow ISO8601>"}'
#    # Check overdue list
#    curl http://localhost:3456/api/v1/tasks?filter=due_date<now+38h
#    ```
#    ## Expected: Task not in overdue list
#    ## Actual: Task appears in overdue list

# 7. Watch the agent post a triage comment, then a fix PR within ~3 minutes.

# 8. For manual run (bypass webhook):
python -m agent.main --issue 42
```
