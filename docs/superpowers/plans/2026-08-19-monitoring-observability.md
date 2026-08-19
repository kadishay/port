# Monitoring & Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record per-issue telemetry (bug type, duration, cost, severity/risk estimates, autonomy outcome, and simulated human review outcomes) to a Supabase Postgres table on every pipeline run, plus a static dashboard to view it.

**Architecture:** A new pure-function module (`agent/telemetry.py`) derives a row dict from the existing `BugContext`/`CostTracker` objects and POSTs it to Supabase's PostgREST endpoint. `orchestrator.py`'s existing `finally` block — which already runs for every terminal branch (not-a-bug, unable-to-reproduce, normal completion, crash) — gets one new call to write that row. A standalone `dashboard.html` reads the table back client-side.

**Tech Stack:** Python (`requests`, already a dependency), Supabase (Postgres + PostgREST), plain HTML/CSS/JS for the dashboard (no framework, no build step).

**Spec:** `docs/superpowers/specs/2026-08-19-monitoring-observability-design.md`

## Global Constraints

- No new Python dependencies — `requests` is already in `requirements.txt`.
- `agent/triage.py` and `agent/solve.py` are not modified — all telemetry logic lives in `agent/telemetry.py` and is wired in from `orchestrator.py` only.
- Telemetry write failures (missing env vars, network errors, non-2xx responses) must never raise out of `record_run` — a broken telemetry write must not break the bug-fixing pipeline.
- `human_merged_as_is`, `human_rejected`, and `human_added_comment` are all simulated (mocked) — the pipeline has no real signal for post-PR GitHub review activity, and the existing HITL reject path in `solve.py` is a pipeline-internal gate, not a proxy for it.
- Dashboard is a single static file, read-only, no server, no build step. Light mode only (explicit simplification — this is a demo/write-up deliverable, not a production surface).

---

### Task 1: `CostTracker.models_used()`

**Files:**
- Modify: `agent/cost_tracker.py`
- Test: `tests/test_cost_tracker.py` (new)

**Interfaces:**
- Produces: `CostTracker.models_used() -> set[str]` — the set of normalized model keys (e.g. `"claude-opus-4-8"`) that have had usage recorded. Used by Task 2's `build_row` to derive `opus_fallback_used`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cost_tracker.py`:

```python
from unittest.mock import MagicMock
from agent.cost_tracker import CostTracker


def test_models_used_returns_normalized_model_keys():
    tracker = CostTracker()
    usage = MagicMock(input_tokens=100, output_tokens=50)
    tracker.record("claude-haiku-4-5-20251001", usage)
    assert tracker.models_used() == {"claude-haiku-4-5"}


def test_models_used_includes_every_recorded_model():
    tracker = CostTracker()
    usage = MagicMock(input_tokens=10, output_tokens=10)
    tracker.record("claude-haiku-4-5-20251001", usage)
    tracker.record("claude-opus-4-8-20260101", usage)
    assert tracker.models_used() == {"claude-haiku-4-5", "claude-opus-4-8"}


def test_models_used_empty_when_nothing_recorded():
    tracker = CostTracker()
    assert tracker.models_used() == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_cost_tracker.py -v`
Expected: FAIL with `AttributeError: 'CostTracker' object has no attribute 'models_used'`

- [ ] **Step 3: Implement `models_used()`**

In `agent/cost_tracker.py`, add this method to the `CostTracker` class (after `record`, before `total_cost`):

```python
    def models_used(self) -> set[str]:
        return set(self._input) | set(self._output)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_cost_tracker.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add agent/cost_tracker.py tests/test_cost_tracker.py
git commit -m "feat: add CostTracker.models_used() for telemetry fallback detection"
```

---

### Task 2: Telemetry pure helpers — bug type, pipeline outcome, mocked human outcomes, row builder

**Files:**
- Create: `agent/telemetry.py`
- Test: `tests/test_telemetry.py` (new)

**Interfaces:**
- Consumes: `CostTracker.models_used() -> set[str]` (Task 1); `BugContext` fields `issue_number`, `issue_title`, `affected_files`, `severity` (`Severity`), `risk_level` (`RiskLevel`), `risk_reasons`, `autonomy_decision` (`AutonomyDecision`), `not_a_bug`, `unable_to_reproduce`, `pr_url` — all defined in `agent/models.py`.
- Produces: `derive_bug_type(affected_files: list[str]) -> str` (`"FE"` / `"BE"` / `"UNKNOWN"`); `derive_pipeline_outcome(ctx: BugContext, crashed: bool) -> str`; `mock_human_outcomes(risk_level: RiskLevel, autonomy_decision: AutonomyDecision) -> dict`; `build_row(ctx: BugContext, tracker: CostTracker, duration_seconds: float, crashed: bool) -> dict`. Task 3 adds `record_run` to this same file, consuming `build_row`. Task 4 (orchestrator) calls `record_run` only.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_telemetry.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telemetry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.telemetry'`

- [ ] **Step 3: Implement the pure helpers**

Create `agent/telemetry.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telemetry.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add agent/telemetry.py tests/test_telemetry.py
git commit -m "feat: add telemetry row derivation (bug type, outcome, mocked human review)"
```

---

### Task 3: `record_run()` — POST to Supabase, plus schema SQL and env vars

**Files:**
- Modify: `agent/telemetry.py` (add `record_run`)
- Create: `agent/telemetry_schema.sql`
- Modify: `.env.sample`
- Test: `tests/test_telemetry.py` (extend)

**Interfaces:**
- Consumes: `build_row(...)` from this same module (Task 2); `requests` (already a dependency); env vars `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
- Produces: `record_run(ctx: BugContext, tracker: CostTracker, duration_seconds: float, crashed: bool = False) -> None`. Task 4 (orchestrator) calls this exact signature.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_telemetry.py`:

```python
import responses as resp_mock
from agent.telemetry import record_run


@resp_mock.activate
def test_record_run_posts_to_supabase(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-key")
    resp_mock.add(
        resp_mock.POST,
        "https://example.supabase.co/rest/v1/bug_runs",
        status=201,
    )
    ctx = _ctx(pr_url="https://github.com/x/y/pull/9")
    tracker = CostTracker()

    record_run(ctx, tracker, duration_seconds=5.0)

    assert len(resp_mock.calls) == 1
    sent = resp_mock.calls[0].request
    assert sent.headers["apikey"] == "test-key"
    assert sent.headers["Authorization"] == "Bearer test-key"


def test_record_run_skips_silently_when_env_missing(monkeypatch, capsys):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    ctx = _ctx()
    tracker = CostTracker()

    record_run(ctx, tracker, duration_seconds=5.0)  # must not raise

    assert "skipping telemetry" in capsys.readouterr().out


@resp_mock.activate
def test_record_run_swallows_request_errors(monkeypatch, capsys):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "test-key")
    resp_mock.add(
        resp_mock.POST,
        "https://example.supabase.co/rest/v1/bug_runs",
        status=500,
    )
    ctx = _ctx()
    tracker = CostTracker()

    record_run(ctx, tracker, duration_seconds=5.0)  # must not raise despite 500

    assert "failed to record run" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_telemetry.py -v -k record_run`
Expected: FAIL with `ImportError: cannot import name 'record_run'`

- [ ] **Step 3: Implement `record_run`, the schema SQL, and env vars**

Append to `agent/telemetry.py` (add `import os` and `import requests` to the top imports):

```python
import os
import requests
```

Then add at the bottom of the file:

```python
_SUPABASE_TABLE = "bug_runs"


def record_run(ctx: BugContext, tracker: CostTracker, duration_seconds: float, crashed: bool = False) -> None:
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print(f"[telemetry] SUPABASE_URL/SUPABASE_SERVICE_KEY not set — skipping telemetry write for #{ctx.issue_number}", flush=True)
        return

    row = build_row(ctx, tracker, duration_seconds, crashed)
    try:
        r = requests.post(
            f"{url}/rest/v1/{_SUPABASE_TABLE}",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            json=row,
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"[telemetry] failed to record run for #{ctx.issue_number}: {e}", flush=True)
```

Create `agent/telemetry_schema.sql` (run this once in the Supabase SQL editor to create the table):

```sql
create table if not exists bug_runs (
    id bigint generated always as identity primary key,
    issue_number integer not null,
    issue_title text not null,
    bug_type text not null,
    created_at timestamptz not null default now(),
    duration_seconds double precision not null,
    cost_usd double precision not null,
    severity text not null,
    risk_level text not null,
    risk_reasons jsonb not null default '[]'::jsonb,
    autonomy_decision text not null,
    opus_fallback_used boolean not null default false,
    pipeline_outcome text not null,
    pr_url text,
    human_merged_as_is boolean,
    human_rejected boolean,
    human_added_comment boolean
);

alter table bug_runs enable row level security;

-- Dashboard reads with the anon key; inserts use the service_role key, which
-- bypasses RLS, so no insert policy is needed here.
create policy "public read access" on bug_runs
    for select
    to anon
    using (true);
```

Add to `.env.sample` (after the Phase 2 Playwright block):

```bash
# Monitoring — Supabase (leave blank to disable; record_run() no-ops if unset)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_SERVICE_KEY=your-service-role-key       # used server-side by record_run(), bypasses RLS
SUPABASE_ANON_KEY=your-anon-key                  # used client-side by dashboard.html, read-only via RLS policy
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_telemetry.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add agent/telemetry.py agent/telemetry_schema.sql .env.sample tests/test_telemetry.py
git commit -m "feat: write telemetry rows to Supabase via record_run()"
```

---

### Task 4: Wire `record_run` into the orchestrator

**Files:**
- Modify: `agent/orchestrator.py`
- Test: `tests/test_orchestrator.py` (extend)

**Interfaces:**
- Consumes: `record_run(ctx: BugContext, tracker: CostTracker, duration_seconds: float, crashed: bool = False) -> None` (Task 3).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator.py`:

```python
@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_records_telemetry_on_normal_completion(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    def fake_triage(ctx, gh, tracker):
        ctx.not_a_bug = True
        return ctx

    with patch("agent.orchestrator.run_triage", side_effect=fake_triage):
        result = run_pipeline(42)

    mock_record.assert_called_once()
    args, kwargs = mock_record.call_args
    assert args[0] is result
    assert kwargs["crashed"] is False
    assert isinstance(args[2], float)  # duration_seconds


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_records_telemetry_with_crashed_true_on_exception(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    with patch("agent.orchestrator.run_triage", side_effect=RuntimeError("boom")):
        run_pipeline(42)

    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["crashed"] is True


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_does_not_record_telemetry_when_pr_already_open(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = {"html_url": "https://github.com/x/y/pull/5"}

    run_pipeline(42)

    mock_record.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_orchestrator.py -v -k telemetry`
Expected: FAIL — `record_run` is not imported/called in `orchestrator.py`, so `mock_record.assert_called_once()` fails with "Expected 'record_run' to have been called once. Called 0 times."

- [ ] **Step 3: Wire it in**

In `agent/orchestrator.py`, add imports at the top (after the existing `from agent.cost_tracker import CostTracker` line):

```python
import time
from agent.telemetry import record_run
```

In `run_pipeline`, capture a start time right after `tracker = CostTracker()`:

```python
    tracker = CostTracker()
    start = time.time()
```

Track whether the run crashed, and call `record_run` in the `finally` block. Change:

```python
    try:
        ctx = run_triage(ctx, gh, tracker)
```

to:

```python
    crashed = False
    try:
        ctx = run_triage(ctx, gh, tracker)
```

Change the `except` block:

```python
    except Exception as e:
        error_msg = f"❌ Pipeline crashed: {type(e).__name__}: {e}"
        print(f"[orchestrator] #{issue_number} — {error_msg}", flush=True)
        gh.post_comment(issue_number, error_msg)
        _notify_thread(ctx, error_msg)
```

to:

```python
    except Exception as e:
        crashed = True
        error_msg = f"❌ Pipeline crashed: {type(e).__name__}: {e}"
        print(f"[orchestrator] #{issue_number} — {error_msg}", flush=True)
        gh.post_comment(issue_number, error_msg)
        _notify_thread(ctx, error_msg)
```

Change the `finally` block:

```python
    finally:
        cost_summary = tracker.summary()
        print(cost_summary, flush=True)
        _notify_thread(ctx, cost_summary)
```

to:

```python
    finally:
        cost_summary = tracker.summary()
        print(cost_summary, flush=True)
        _notify_thread(ctx, cost_summary)
        record_run(ctx, tracker, time.time() - start, crashed=crashed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_orchestrator.py -v`
Expected: all tests pass (5 total: 2 pre-existing + 3 new)

- [ ] **Step 5: Run the full test suite to check nothing else broke**

Run: `python -m pytest tests/ -v -m "not integration"` (or `python -m pytest tests/ --ignore=tests/test_demo_bugs_integration.py -v` if no marker exists — check `pytest.ini` for the actual exclusion pattern used elsewhere in this repo)
Expected: all non-integration tests pass

- [ ] **Step 6: Commit**

```bash
git add agent/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: record telemetry for every pipeline run via orchestrator finally block"
```

---

### Task 5: Dashboard

**Files:**
- Create: `dashboard.html`

**Interfaces:**
- Consumes: Supabase PostgREST GET on `bug_runs` (the table created by Task 3's `agent/telemetry_schema.sql`), authenticated with the anon key via the `SUPABASE_ANON_KEY` policy added in that same task.
- No automated test — this is a static file with no build step. Verification is manual (Step 2 below).

- [ ] **Step 1: Create `dashboard.html`**

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bug Triage — Monitoring Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --surface-1: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --text-muted: #898781;
    --gridline: #e1e0d9;
    --baseline: #c3c2b7;
    --series-be: #2a78d6;
    --series-fe: #eb6834;
    --ord-1: #86b6ef;
    --ord-2: #5598e7;
    --ord-3: #2a78d6;
    --ord-4: #184f95;
    --border: rgba(11,11,11,0.10);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text-primary);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 32px;
  }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); margin: 0 0 24px; }
  .config-banner {
    background: #fff4e5;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    margin-bottom: 20px;
    display: none;
  }
  .card {
    background: var(--surface-1);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 20px;
  }
  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .stat-value {
    font-size: 28px;
    font-weight: 600;
    font-variant-numeric: normal;
  }
  .stat-label {
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.02em;
    margin-top: 4px;
  }
  .charts {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 24px;
  }
  .chart-title { font-weight: 600; margin: 0 0 12px; }
  .bar-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .bar-label { width: 90px; flex-shrink: 0; color: var(--text-secondary); font-size: 12px; }
  .bar-track { flex: 1; background: var(--gridline); border-radius: 4px; height: 14px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; }
  .bar-value { width: 48px; text-align: right; color: var(--text-secondary); font-size: 12px; font-variant-numeric: tabular-nums; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--gridline); }
  th { color: var(--text-muted); font-weight: 600; text-transform: uppercase; font-size: 11px; letter-spacing: 0.02em; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  a { color: var(--series-be); }
  .table-wrap { overflow-x: auto; }
</style>
</head>
<body>
  <h1>Bug Triage — Monitoring Dashboard</h1>
  <p class="subtitle">Per-issue cost, duration, and outcome telemetry from the bug-triage pipeline.</p>

  <div id="config-banner" class="config-banner card">
    Set <code>SUPABASE_URL</code> and <code>SUPABASE_ANON_KEY</code> at the top of this file's
    <code>&lt;script&gt;</code> block, then reload.
  </div>

  <div class="stat-grid" id="stats"></div>

  <div class="charts">
    <div class="card">
      <p class="chart-title">Avg cost by bug type</p>
      <div id="chart-bug-type"></div>
    </div>
    <div class="card">
      <p class="chart-title">Severity distribution</p>
      <div id="chart-severity"></div>
    </div>
    <div class="card">
      <p class="chart-title">Merge rate by risk level</p>
      <div id="chart-merge-rate"></div>
    </div>
    <div class="card">
      <p class="chart-title">Autonomy decisions</p>
      <div id="chart-autonomy"></div>
    </div>
  </div>

  <div class="card table-wrap">
    <table id="runs-table">
      <thead>
        <tr>
          <th>Issue</th><th>Type</th><th>Severity</th><th>Risk</th>
          <th>Autonomy</th><th>Outcome</th><th class="num">Cost</th>
          <th class="num">Duration</th><th>PR</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

<script type="module">
  // Fill these in from your Supabase project settings (Project Settings → API).
  const SUPABASE_URL = "";
  const SUPABASE_ANON_KEY = "";

  const SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
  const RISK_ORDER = ["LOW", "MEDIUM", "HIGH", "ESCALATE"];
  const ORDINAL_COLORS = ["var(--ord-1)", "var(--ord-2)", "var(--ord-3)", "var(--ord-4)"];

  function bar(label, valueLabel, fraction, color) {
    const row = document.createElement("div");
    row.className = "bar-row";
    row.innerHTML = `
      <div class="bar-label">${label}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.round(fraction * 100)}%;background:${color}"></div></div>
      <div class="bar-value">${valueLabel}</div>
    `;
    return row;
  }

  function statTile(value, label) {
    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = `<div class="stat-value">${value}</div><div class="stat-label">${label}</div>`;
    return div;
  }

  function renderStats(runs) {
    const el = document.getElementById("stats");
    el.innerHTML = "";
    const avgCost = runs.length ? runs.reduce((s, r) => s + r.cost_usd, 0) / runs.length : 0;
    const avgDuration = runs.length ? runs.reduce((s, r) => s + r.duration_seconds, 0) / runs.length : 0;
    const merged = runs.filter(r => r.human_merged_as_is).length;
    el.append(
      statTile(runs.length, "Total runs"),
      statTile(`$${avgCost.toFixed(3)}`, "Avg cost / run"),
      statTile(`${avgDuration.toFixed(0)}s`, "Avg duration"),
      statTile(runs.length ? `${Math.round((merged / runs.length) * 100)}%` : "—", "Merged as-is"),
    );
  }

  function renderBugType(runs) {
    const el = document.getElementById("chart-bug-type");
    el.innerHTML = "";
    for (const [type, color] of [["BE", "var(--series-be)"], ["FE", "var(--series-fe)"]]) {
      const subset = runs.filter(r => r.bug_type === type);
      const avg = subset.length ? subset.reduce((s, r) => s + r.cost_usd, 0) / subset.length : 0;
      const maxCost = Math.max(0.001, ...runs.map(r => r.cost_usd));
      el.appendChild(bar(type, `$${avg.toFixed(3)}`, avg / maxCost, color));
    }
  }

  function renderOrdinal(elId, runs, field, order) {
    const el = document.getElementById(elId);
    el.innerHTML = "";
    const counts = order.map(level => runs.filter(r => r[field] === level).length);
    const max = Math.max(1, ...counts);
    order.forEach((level, i) => el.appendChild(bar(level, String(counts[i]), counts[i] / max, ORDINAL_COLORS[i])));
  }

  function renderMergeRate(runs) {
    const el = document.getElementById("chart-merge-rate");
    el.innerHTML = "";
    RISK_ORDER.forEach((level, i) => {
      const subset = runs.filter(r => r.risk_level === level);
      const rate = subset.length ? subset.filter(r => r.human_merged_as_is).length / subset.length : 0;
      el.appendChild(bar(level, `${Math.round(rate * 100)}%`, rate, ORDINAL_COLORS[i]));
    });
  }

  function renderTable(runs) {
    const tbody = document.querySelector("#runs-table tbody");
    tbody.innerHTML = "";
    for (const r of runs) {
      const tr = document.createElement("tr");
      const prCell = r.pr_url ? `<a href="${r.pr_url}" target="_blank">PR</a>` : "—";
      tr.innerHTML = `
        <td>#${r.issue_number} ${r.issue_title}</td>
        <td>${r.bug_type}</td>
        <td>${r.severity}</td>
        <td>${r.risk_level}</td>
        <td>${r.autonomy_decision}</td>
        <td>${r.pipeline_outcome}</td>
        <td class="num">$${r.cost_usd.toFixed(4)}</td>
        <td class="num">${r.duration_seconds.toFixed(0)}s</td>
        <td>${prCell}</td>
      `;
      tbody.appendChild(tr);
    }
  }

  async function main() {
    if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
      document.getElementById("config-banner").style.display = "block";
      return;
    }
    const res = await fetch(`${SUPABASE_URL}/rest/v1/bug_runs?select=*&order=created_at.desc`, {
      headers: {
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
      },
    });
    const runs = await res.json();
    renderStats(runs);
    renderBugType(runs);
    renderOrdinal("chart-severity", runs, "severity", SEVERITY_ORDER);
    renderMergeRate(runs);
    renderOrdinal("chart-autonomy", runs, "autonomy_decision", ["AUTO_PR", "HITL_REQUIRED"]);
    renderTable(runs);
  }

  main();
</script>
</body>
</html>
```

- [ ] **Step 2: Manually verify**

Open `dashboard.html` directly in a browser (`open dashboard.html` on macOS). With `SUPABASE_URL`/`SUPABASE_ANON_KEY` left blank, confirm the config banner shows and nothing else errors in the console. After Task 3's SQL has been run in a real Supabase project and at least one row exists (from a real `--issue N` run or a manually inserted test row), fill in the two constants, reload, and confirm the stat tiles, all four bar charts, and the table populate without console errors.

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: add static Supabase-backed monitoring dashboard"
```

---

## After This Plan

To actually see data end-to-end: create a Supabase project, run `agent/telemetry_schema.sql` in its SQL editor, copy the project URL + `service_role` key into `.env` as `SUPABASE_URL`/`SUPABASE_SERVICE_KEY`, run `python -m agent.main --issue <N>` against one of the two demo bugs, then fill in `dashboard.html`'s `SUPABASE_URL`/`SUPABASE_ANON_KEY` constants and open it in a browser.
