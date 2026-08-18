# Bug Triage & Solve Agent — Design Spec

**Date:** 2026-08-18  
**Status:** Approved  
**Target app:** Vikunja fork (`/Users/kadishay/Code/vikunja`)  
**Agent repo:** `https://github.com/kadishay/port`

---

## Overview

An agentic workflow that monitors a GitHub repository for bug issues, reproduces them against a running Vikunja instance, classifies their severity, and — depending on confidence and risk — either merges a fix autonomously, requests human approval via Slack, or escalates to a human entirely.

---

## Delivery Phases

| Phase | Scope | Status |
|-------|-------|--------|
| **1 — MVP** | Local agent, GitHub webhook, GitHub-based HITL | In scope |
| **2 — Slack** | Slack notifications + bidirectional Slack-based HITL | In scope |
| **3 — Optional** | Production deployment; codebase memory & cross-bug learning | Optional |

---

## Scope

**Phase 1 (MVP):**
- Triage: reproduce → root cause → classify severity → route
- Solve: propose → autonomy decision → apply → test → PR (or HITL gate)
- Two pre-introduced bugs: 1 backend (Go), 1 frontend (Vue 3)

**Phase 2 (Slack + Frontend Reproduction):**
- Agent posts status updates to Slack channel at every major step
- Human escalation happens in Slack, not GitHub comments
- Humans can respond in Slack to approve/reject fixes or ask questions
- Autonomy policy defined explicitly (see Autonomy Decision Matrix)
- **Playwright integration for frontend bug reproduction and verification:**
  - Phase 1 can only reproduce backend bugs via API calls; frontend bugs are inspected via source code, not actually executed
  - Phase 2 adds a Playwright (headless Chromium) driver as a triage tool — the agent can navigate the Vikunja UI, perform the steps described in the issue, take a screenshot as proof of the bug, and attach it to the GitHub triage comment
  - After the fix is applied, Playwright re-runs the same steps to verify the bug is gone before the PR is opened
  - Requires the frontend dev server (`pnpm dev`, port 4173) to be running alongside the backend

**Phase 3 (Optional):**
- Move agent + Vikunja to cloud (Fly.io / Railway / Docker Compose on a VPS)
- Persistent memory across bugs: agent learns codebase structure over time
- See Phase 3 section at the bottom for details

**Not in scope (any phase):**
- Multi-repo support, flaky-test handling, security scanning

---

## Bugs to Introduce

### Backend Bug (Go)
**File:** `pkg/models/task.go`  
**Change:** In the `isTaskOverdue` helper, change the comparison from `task.DueDate.Before(now)` to `task.DueDate.Before(now.Add(24 * time.Hour))` — tasks due _today_ are incorrectly flagged as overdue.  
**Why it's a good demo bug:**
- Reproducible via a single API call (`GET /api/v1/tasks?filter=overdue`)
- Root cause is a one-liner logic error
- Has a clear, minimal fix
- Affects real user-visible behavior

### Frontend Bug (Vue 3)
**File:** `frontend/src/stores/tasks.ts`  
**Change:** In the `fetchTasks` action, use `task.done === false` instead of `!task.done` to filter incomplete tasks — this fails for tasks where `done` is `undefined`, incorrectly hiding them.  
**Why it's a good demo bug:**
- Reproducible by creating a task without explicitly setting `done`
- Root cause is a type-coercion gotcha (`undefined === false` is `false`)
- Fix is a one-liner
- Frontend-only: no backend change needed

---

## Architecture

### Phase 1 (Local + GitHub)

```
GitHub Issue (opened)
       │
       ▼
[Webhook Receiver] ─── ngrok tunnel → localhost:9090
       │
       ▼
[Orchestrator] — Python, Anthropic SDK
       │
       ├─[Triage Agent]──────────────────────────────────────┐
       │   Model: claude-haiku-4-5                            │
       │   1. Parse issue body → structured bug report        │
       │   2. Set up Vikunja (start services if needed)       │
       │   3. Execute reproduction steps via shell tool        │
       │   4. Collect stdout/stderr/API responses             │
       │   5. Model: claude-opus-4-8 → root cause analysis   │
       │   6. Model: claude-haiku-4-5 → severity + confidence │
       │   7. Post triage comment to GitHub issue             │
       │                                                      │
       │         severity ≥ HIGH?                             │
       │              │                                       │
       │         ┌────┴────┐                                 │
       │         YES       NO ───────────────────────────────┤
       │         │                   [Label + assign human]   │
       ▼         ▼                                            │
[Solve Agent]  [Label + route to human]                      │
   Model: claude-opus-4-8                                    │
   1. Propose fix (file diffs)                               │
   2. Run autonomy check (see matrix below)                  │
      ├── AUTO MERGE: apply + test + PR (no human needed)    │
      └── HITL: post diff to GitHub, poll for /approve       │
   3. If approved: apply patch, run tests, open PR           │
   4. If rejected / timeout: post explanation, stop          │
```

### Phase 2 (+ Slack)

Slack replaces GitHub comment polling for all human interaction:

```
[Solve Agent]
   │
   ├── Post to #bug-triage Slack channel:
   │     "🔍 Bug #42: tasks due today shown as overdue
   │      Severity: HIGH | Confidence: 0.91
   │      Root cause: off-by-one in isTaskOverdue()
   │      Proposed fix: [diff link]
   │      → Auto-merging (meets autonomy criteria)"
   │
   ├── OR (HITL required):
   │     "⚠️ Bug #7: auth middleware bypass
   │      Severity: CRITICAL | Confidence: 0.78
   │      Proposed fix: [diff link]
   │      Reply `approve` or `reject` within 30 min"
   │
   └── Human replies in Slack thread → agent reads + acts

[Status updates at every step]
   "📥 Received issue #42 — starting triage"
   "🔬 Reproduced: API returns 5 overdue tasks, expected 3"
   "🧠 Root cause identified: isTaskOverdue() adds 24h offset"
   "🔧 Fix proposed: 1 file, 1 line changed"
   "✅ Tests passing — merging automatically"
   "🔀 PR #8 opened: fix/task-overdue-comparison"
```

---

## Two Orthogonal Metrics

**Severity** and **Risk** are distinct concepts that each drive a different decision:

| Metric | Question it answers | Drives |
|--------|-------------------|--------|
| **Severity** | How critical is this bug to fix? | Whether and how urgently to attempt a fix |
| **Risk** | How dangerous is the proposed fix? | Whether to auto-merge or require human approval |

A bug can be high-severity + low-risk (a one-line typo in a core function → fix it immediately, merge automatically) or low-severity + high-risk (a cosmetic tweak that touches auth middleware → not urgent, but needs human eyes). These must be evaluated independently.

---

## Severity Classification

Drives triage routing and urgency.

| Severity | Criteria | Triage action | Fix action |
|----------|----------|--------------|------------|
| CRITICAL | Data loss, auth bypass, crash on startup | Alert human immediately (Slack + GitHub) | Attempt fix → HITL always |
| HIGH | Wrong data shown, core feature broken | Post triage comment | Evaluate risk → auto-merge or HITL |
| MEDIUM | Degraded UX, edge case error | Post triage comment | Evaluate risk → auto-merge or HITL |
| LOW | Visual glitch, non-blocking | Label only | No action |

---

## Risk Classification

Evaluated by the Solve Agent after the fix is proposed. Drives the merge decision.

| Risk | Criteria | Decision |
|------|----------|----------|
| **LOW** | ≤ 2 files, ≤ 15 lines, pure logic correction, confidence ≥ 0.85, no new deps, tests pass | AUTO_MERGE |
| **MEDIUM** | 3–5 files, 16–50 lines, some refactoring, confidence 0.70–0.84 | HITL_REQUIRED |
| **HIGH** | > 5 files, > 50 lines, new behavior, confidence < 0.70, test files modified | HITL_REQUIRED |
| **ESCALATE** | Auth/permissions, DB migrations, API contract changes, session tokens | ESCALATE_ONLY (no auto-fix) |

**Risk is independent of severity.** A CRITICAL bug can have LOW risk (and still requires HITL because of severity). A LOW severity bug can have LOW risk and be auto-merged.

---

## Autonomy Decision Matrix

Combines both metrics:

```
Severity = CRITICAL  →  HITL always (regardless of risk)
Severity = HIGH/MED  →  evaluate risk:
    Risk = LOW       →  AUTO_MERGE
    Risk = MEDIUM    →  HITL_REQUIRED
    Risk = HIGH      →  HITL_REQUIRED
    Risk = ESCALATE  →  ESCALATE_ONLY
Severity = LOW       →  label only, no fix
```

**On CRITICAL bugs:** the agent does _both_ — alerts human immediately AND starts working on a fix. Posts the proposed fix for human review but never merges automatically, even if risk is LOW.

Both demo bugs (HIGH severity, 1 file, 1 line, confidence ≥ 0.85) → LOW risk → AUTO_MERGE.

---

## Components

### Phase 1

#### `agent/main.py`
Entry point. Starts the webhook server and dispatches events to the orchestrator.

#### `agent/orchestrator.py`
Reads the GitHub issue, decides triage vs. solve path, manages state across steps. Passes structured `BugContext` to sub-agents.

#### `agent/triage.py`
Triage sub-agent using the Anthropic SDK tool runner. Tools:
- `run_shell(cmd)` — executes shell commands against the Vikunja repo
- `read_file(path)` — reads source files for context
- `post_github_comment(issue, body)` — posts triage results

#### `agent/solve.py`
Solve sub-agent. Manual agentic loop (not tool runner) so we can pause at autonomy decision points. Tools:
- `run_shell(cmd)` — runs tests
- `write_file(path, content)` — applies patch
- `git_diff()` — produces diff for review
- `create_pr(title, body, branch)` — opens PR
- `post_github_comment(issue, body)` — posts proposed fix + approval request

#### `agent/autonomy.py`
Evaluates the `BugContext` + proposed diff against the autonomy matrix. Returns `AUTO_MERGE` or `HITL_REQUIRED` with reasoning.

#### `agent/webhook_server.py`
FastAPI app listening on `:9090`. Validates GitHub webhook signature, enqueues events.

### Phase 2 additions

#### `agent/slack_client.py`
Wraps `slack-sdk`. Handles:
- Posting status updates to `#bug-triage`
- Listening for human replies in threads (Slack Bolt app, socket mode)
- Mapping Slack thread replies to the correct `BugContext`

#### `agent/hitl.py`
Unified HITL handler. In Phase 1: polls GitHub comments. In Phase 2: waits on Slack thread reply. Both implement the same interface: `await hitl.wait_for_approval(context, timeout=1800)`.

---

## Model Assignment

| Step | Model | Reason |
|------|-------|--------|
| Parse issue body | Haiku | Cheap extraction |
| Root cause analysis | Opus 4.8 + thinking | Code reading + deep reasoning |
| Severity + confidence score | Haiku (structured output) | Simple rubric, fast |
| Fix proposal | Opus 4.8 + thinking | Code generation quality matters |
| Autonomy check | Haiku | Rule evaluation, no reasoning needed |
| Test result interpretation | Haiku | Pass/fail parsing |

---

## Trigger Mechanism

**Primary:** GitHub webhook → ngrok tunnel → local FastAPI server on port **9090**.

Port 9090 avoids conflicts with common dev server defaults (Vikunja: 3456, frontend: 4173, common dev: 3000/8080).

Setup: `ngrok http 9090` → configure webhook URL in the GitHub repo settings.

**Fallback (manual):** `python agent/main.py --issue <number>` bypasses the webhook and runs the full pipeline for a given issue number directly.

---

## Context Passing

Each step passes a `BugContext` dataclass downstream:

```python
@dataclass
class BugContext:
    issue_number: int
    issue_title: str
    issue_body: str
    repo_path: str            # /Users/kadishay/Code/vikunja
    reproduction_log: str     # stdout/stderr from reproduction
    root_cause: str           # Opus analysis
    severity: str             # CRITICAL/HIGH/MEDIUM/LOW
    confidence: float         # 0.0–1.0 from Opus structured output
    proposed_diff: str        # git diff of proposed fix
    autonomy_decision: str    # AUTO_MERGE / HITL_REQUIRED / ESCALATE
    autonomy_reasons: list[str]
    slack_thread_ts: str      # Slack thread ID for Phase 2 HITL
```

Phase 1: context is in-process only. Phase 2: persisted to a local SQLite file so the Slack listener can match replies back to the right context.

---

## Vikunja Stack Assumptions

- Backend runs on `localhost:3456` (default Vikunja port)
- Frontend runs on `localhost:4173` (pnpm dev)
- SQLite for test isolation (avoids needing Postgres running)
- Agent starts Vikunja via `mage build && ./vikunja` before reproduction

---

## Repository Structure

```
port/
├── agent/
│   ├── main.py
│   ├── orchestrator.py
│   ├── triage.py
│   ├── solve.py
│   ├── autonomy.py
│   ├── hitl.py
│   ├── slack_client.py        # Phase 2
│   ├── models.py              # BugContext dataclass
│   ├── webhook_server.py
│   └── tools/
│       ├── github_tools.py
│       ├── shell_tools.py
│       └── file_tools.py
├── bugs/
│   ├── introduce_bugs.sh      # applies both bugs to the Vikunja fork
│   └── revert_bugs.sh         # removes bugs (for reset between demos)
├── docs/
│   └── superpowers/specs/
│       └── 2026-08-18-bug-triage-agent-design.md
├── requirements.txt
├── .env.sample                # ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_WEBHOOK_SECRET,
│                              # SLACK_BOT_TOKEN, SLACK_APP_TOKEN (Phase 2)
└── AI Director Task.pdf
```

---

## Cost Estimate

Per full pipeline run (triage + solve):
- Issue parsing (Haiku): ~500 input tokens → <$0.001
- Root cause analysis (Opus, thinking): ~5K input + 2K output → ~$0.08
- Fix proposal (Opus, thinking): ~8K input + 3K output → ~$0.12
- Misc Haiku calls: ~$0.002

**Total per bug: ~$0.20** — well within $50 budget for many demo runs.

---

## Success Criteria

**Phase 1:**
1. GitHub issue opened → triage comment posted within 2 minutes.
2. Triage comment includes: reproduction result, root cause, severity, confidence score.
3. For HIGH severity with auto-merge criteria met: PR opened automatically, tests passing, no human interaction needed.
4. For HIGH severity with HITL: proposed fix diff posted to GitHub, waits for `/approve`.
5. Full pipeline runs locally without CI.

**Phase 2:**
6. Every pipeline step produces a Slack message in `#bug-triage`.
7. HITL approval/rejection can be done entirely from Slack (no need to touch GitHub comments).
8. CRITICAL bugs trigger an immediate Slack alert before any fix is attempted.

---

## Phase 3 (Optional)

### Production Deployment

Move the agent and Vikunja to a cloud environment so the pipeline runs without a laptop:

- **Vikunja:** Docker Compose on a VPS (Fly.io / Railway / Hetzner). Backend + Postgres + frontend served via nginx.
- **Agent:** Deployed as a long-running process alongside Vikunja (same Docker Compose, separate service). No ngrok needed — public IP directly.
- **Webhook:** GitHub webhook points directly to the cloud host's IP/domain.
- **Secrets:** Managed via the cloud provider's secrets store (Fly secrets / Railway env vars).

The agent connects to the Vikunja instance over the internal Docker network for reproduction steps.

### Codebase Memory & Cross-Bug Learning

How does the agent understand the codebase, and how does solving bug #1 help it solve bug #2 faster?

**Codebase understanding (static context):**
- On first run, the agent indexes key Vikunja files: model layer, route layer, store layer. Summaries (not full source) are stored in a local vector DB (ChromaDB or SQLite-vec).
- When a bug arrives, relevant files are retrieved by similarity to the bug description before Opus starts analysis — reducing tokens needed and improving focus.

**Cross-bug learning (dynamic context):**
- After each resolved bug, a `Resolution` record is saved: bug pattern, root cause category, files changed, fix pattern, confidence.
- On new bugs, the agent queries past resolutions for similar patterns: "off-by-one in date comparison" → retrieve resolution from bug #1, include as few-shot context for Opus.
- Over time this builds a lightweight "playbook" of known fix patterns in this codebase.

**What this improves:**
- Bug #2 root cause analysis is faster (fewer Opus tokens, shorter thinking) because the fix pattern is already known.
- Confidence scores improve because the agent can compare against past successful fixes.
- The playbook is inspectable (plain JSON file) — useful for the video walkthrough.
