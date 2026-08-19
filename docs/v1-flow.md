# V1 System Flow — Bug Triage & Solve Agent

This document walks through the complete lifecycle of a bug, from a GitHub issue being opened to a pull request being merged (or a human being notified). Every step maps to actual code in `agent/`.

---

## Entry Points

There are two ways to trigger the pipeline:

| Mode | Command | When to use |
|------|---------|-------------|
| **Manual** | `python -m agent.main --issue 42` | Testing, demos, re-running a specific issue |
| **Webhook server** | `python -m agent.main --serve` | Live operation — listens on port 9090 |

Both modes call the same `run_pipeline(issue_number)` function in `orchestrator.py`.

---

## Step 0 — Webhook Reception (server mode only)

`agent/webhook_server.py` runs a FastAPI app on port 9090. GitHub sends a POST to `/webhook` every time an issue event fires.

**Validation:** The server verifies the `X-Hub-Signature-256` header using HMAC-SHA256 with `GITHUB_WEBHOOK_SECRET`. If the signature doesn't match, the request is rejected with 403.

**Filtering:** Only `issues` events with `action = "opened"` proceed. Everything else (closed, labeled, commented, etc.) is ignored and returns 200.

**Dispatch:** A daemon thread is started for `run_pipeline(issue_number)`. The webhook returns 202 immediately — GitHub doesn't wait for the pipeline to finish.

```
GitHub POST /webhook
    │
    ├── Signature valid? ──No──▶ 403 Forbidden
    │
    ├── Event = "issues" + action = "opened"? ──No──▶ 200 Ignored
    │
    └── spawn daemon thread ──▶ run_pipeline(N) ──▶ 202 Accepted
```

---

## Step 1 — Orchestrator Sets Up Context

`agent/orchestrator.py` → `run_pipeline(issue_number)`

1. Creates a `GitHubClient` and fetches the issue via the GitHub REST API.
2. Builds a `BugContext` dataclass — the single object passed through the entire pipeline:
   ```
   BugContext(
     issue_number, issue_title, issue_body,
     repo_path=/Users/kadishay/Code/vikunja,
     reproduction_log="", root_cause="", severity=MEDIUM,
     confidence=0.0, proposed_diff="",
     risk_level=MEDIUM, risk_reasons=[],
     autonomy_decision=HITL_REQUIRED, autonomy_reasons=[],
     fix_branch="", pr_url=""
   )
   ```
3. Logs/notifies receipt of the issue (prints to stdout; Slack if `SLACK_BOT_TOKEN` is set).

---

## Step 2 — Triage Agent

`agent/triage.py` → `run_triage(ctx, gh)`

Triage runs five sequential sub-steps across two models:

### 2a. Parse Reproduction Steps (Haiku)


**Model:** `claude-haiku-4-5`  
**Input:** Issue title + body  
**Output:** A plain-text list of shell/curl commands needed to reproduce the bug

Haiku is cheap and fast for this structured extraction task.

### 2b. Reproduce the Bug (Haiku + tool loop)

**Model:** `claude-haiku-4-5` with tools  
**Tools available:**
- `run_shell(cmd, timeout)` — executes any shell command; runs in the Vikunja repo directory
- `read_file(path)` — reads source files from the Vikunja repo

**How it works:** The model runs in a loop. Each iteration:
1. Model decides what tool to call next (a shell command, a file read).
2. Tool result is fed back as a `tool_result` message.
3. Loop exits when `stop_reason = "end_turn"` (model says it's done observing).

**Output:** `ctx.reproduction_log` — raw stdout/stderr from all tool calls, plus the model's final summary of what it observed.

**When `PLAYWRIGHT_ENABLED=true`**, six additional browser tools are available alongside `run_shell` and `read_file`:

| Tool | What it does |
|------|-------------|
| `browser_navigate(url)` | Navigate to a URL (Vikunja frontend at `:4173`) |
| `browser_click(selector)` | Click an element by CSS or text selector |
| `browser_type(selector, text)` | Fill an input field |
| `browser_get_text(selector)` | Read visible text from an element |
| `browser_screenshot(filename)` | Take a screenshot — path saved to `ctx.screenshot_before` |
| `browser_wait(ms)` | Wait for animations or async updates |

Haiku decides whether to use the browser based on the issue body. For backend bugs it uses `run_shell`. For UI bugs it navigates to the frontend, performs the reproduction steps, and calls `browser_screenshot` to capture proof. The browser is closed after the reproduce step — the screenshot path is included in the triage comment.

**When `PLAYWRIGHT_ENABLED=false` (default):** browser tools are not offered to the model. Frontend bugs fall back to source code inspection via `read_file`. No Playwright installation required.

**Requires:** `pip install playwright && playwright install chromium` + Vikunja frontend running (`pnpm dev`, port 4173).

### 2c. "Not a Bug" Check (Haiku + tool loop)

**Model:** `claude-haiku-4-5` with tools  
**Tools available:** `read_file`, `run_shell` (same as 2b)

Haiku reads the Vikunja documentation and relevant source files to determine whether the reported behaviour is actually the intended, documented behaviour.

**Output:** JSON `{"not_a_bug": true/false, "reason": "..."}`

If `not_a_bug = true`:
- A comment is posted explaining why it's expected behaviour, with the reason
- A `not-a-bug` label is added to the issue
- Triage ends here — root cause analysis and the solve pipeline are skipped entirely

This check runs before Opus to avoid spending expensive tokens on issues that aren't actually bugs.

### 2d. Root Cause Analysis (Haiku + read_file tools)

**Model:** `claude-haiku-4-5` with tools (capped at 8 iterations)
**Input:** Issue title + reproduction log  
**Output:** JSON `{"root_cause": "...", "confidence": 0.XX, "files": [...], "buggy_pattern": "..."}`

Haiku reads the most relevant source file(s) via `read_file` (up to 2 reads), then returns a JSON diagnosis. For single-file logic bugs the root cause is apparent from the code — deep reasoning isn't needed. Haiku completes this in ~5–10s vs 30–60s+ for Opus with adaptive thinking, at ~5× lower cost. The confidence score (0.0–1.0) reflects how certain Haiku is. The `files` and `buggy_pattern` fields are used by the next step to trace authorship.

```
ctx.root_cause     = "The done filter uses 'done = true' instead of 'done = false'..."
ctx.confidence     = 0.95
ctx.affected_files = ["pkg/models/task_overdue_reminder.go"]
ctx.buggy_pattern  = "done = true"
```

### 2e. Find Relevant People (git + GitHub API)

**No model — pure git + GitHub REST API calls**

After the root cause is known, the agent traces two things:

**Bug introducer** — uses `git log -S "<buggy_pattern>" -- <filepath>` (the "pickaxe") to find the exact commit that introduced the buggy string, regardless of when that was. `git log -1` would only find the most recent commit to the file, which could be a formatting change unrelated to the bug. The pickaxe finds the specific commit that added the problematic line.

```bash
git log -S "time.Hour*38" --format=%H -- pkg/models/task_overdue_reminder.go
# → commit SHA d3f1a...
GET /repos/kadishay/vikunja/commits/d3f1a...
# → author.login: "dev-who-introduced-it"
```

**Area experts** — fetches the last 100 commits touching the affected file via the GitHub API, counts commits per author, and returns the top 2 by commit count (excluding the bug introducer to avoid duplication).

```
ctx.blame_author = "dev-who-introduced-it"
ctx.area_experts = ["top-contributor-1", "top-contributor-2"]
```

Both are included in the triage comment as `@mention`s and in the Slack notification (Phase 2).

### 2f. Severity Classification (Haiku)

**Model:** `claude-haiku-4-5`  
**Input:** Root cause string  
**Output:** JSON `{"severity": "HIGH"}`  

Haiku applies a four-point rubric:
- `CRITICAL` — data loss, auth bypass, crash on startup
- `HIGH` — wrong data shown, core feature broken
- `MEDIUM` — degraded UX, edge case error
- `LOW` — visual glitch, non-blocking

### 2g. Triage Comment Posted

A formatted comment is posted to the GitHub issue with:
- Severity badge + emoji
- Confidence percentage
- Root cause explanation
- **People to notify** — `@blame_author (introduced the bug)`, `@expert1 (area expert)`, `@expert2 (area expert)`
- Reproduction log (truncated to 2000 chars)

---

## Step 3 — Label and Hand Off to Solve

After triage, the orchestrator adds a severity label to the issue (`severity:high`, `severity:low`, etc.) and then always proceeds to the solve pipeline. Every bug gets a fix attempt — severity informs urgency and the HITL rule for CRITICAL, but it never gates whether the agent tries to fix the bug. That decision belongs to Risk.

---

## Step 4 — Solve Agent

`agent/solve.py` → `run_solve(ctx, gh)`

### 4a. Create Fix Branch

```bash
git -C <repo_path> checkout -b fix/issue-<N>
```

### 4b. Apply the Fix (Haiku + tool loop)

**Model:** `claude-haiku-4-5`  
**Tools available:** same `read_file`, `write_file`, `run_shell` as triage

Haiku finds the exact file, checks recent git history to recover the original value before the bug was introduced, then writes the minimal fix and runs tests. The fix strategy:

1. **Locate** — grep for the buggy pattern in `pkg/models/` or `frontend/src/` to confirm the file.
2. **Recover original** — `git log -5 -- <file>` then `git show <prev-sha>:<path>` to see what the line looked like before the bug commit. This ensures the fix restores the intended value (e.g. `time.Hour*14`) rather than guessing a simpler replacement.
3. **Change exactly one value** — only the identified buggy value is replaced; no reformatting, no nearby fixes, no new functions.
4. **Verify** — `run_shell` to build and test.

The loop exits when Haiku calls `end_turn`, capped at 8 iterations.

### 4b². Verify Fix with Playwright (optional)

**Only runs when:** `PLAYWRIGHT_ENABLED=true` AND a before-screenshot was captured during triage.

Haiku re-runs the reproduction steps using the same browser tools, navigating back to the same view in the Vikunja frontend. It takes an after-screenshot (`bug-N-after.png`) showing the fixed state. Both screenshots (before + after) are included in the PR body as local file paths.

If Playwright is disabled or the bug was backend-only (no before-screenshot), this step is skipped entirely.

### 4c. Capture the Diff

```python
diff = git_diff(ctx.repo_path)   # git diff of all unstaged changes
ctx.proposed_diff = diff
```

### 4d. Evaluate Risk (no model — pure Python)

Two independent evaluations happen:

**Risk evaluation** (`evaluate_risk(diff, ctx)`):

| Check | LOW | MEDIUM | HIGH |
|-------|-----|--------|------|
| Files changed | ≤ 2 | 3–5 | > 5 |
| Lines changed | ≤ 15 | 16–50 | > 50 |
| Confidence | ≥ 0.85 | 0.70–0.84 | < 0.70 |
| Escalate keywords | — | — | auth token, migration, schema alter... |

`ctx.risk_level` and `ctx.risk_reasons` are written back to the context.

**Autonomy decision** (`evaluate_autonomy(ctx, diff)`):

```
ESCALATE keyword in diff   →  ESCALATE_ONLY
severity = CRITICAL        →  HITL_REQUIRED (always, even if LOW risk)
risk = LOW                 →  AUTO_MERGE
risk = MEDIUM or HIGH      →  HITL_REQUIRED
```

### 4e. Solution Decision

The output of `evaluate_autonomy()` routes to one of three paths:

#### ESCALATE_ONLY
**Selected when:** the diff contains an escalation keyword (`auth token`, `auth middleware`, `session token`, `migration`, `schema alter`). This check runs before any other rule — if the keyword is present, the path is forced regardless of severity, risk level, or confidence.

**What happens:**
1. Posts a comment explaining the fix touches auth/migrations/API contracts and will not be applied automatically.
2. No code is committed. No branch is pushed. The dirty working tree is left as-is.

---

#### AUTO_MERGE
**Selected when:** risk level is LOW (rules + Haiku both agree) AND severity is not CRITICAL.

**What happens:**
1. `git add -A && git commit -m "fix: resolve #N - <title>"`
2. `git push origin fix/issue-N`
3. `gh.create_pr(...)` — opens a PR via the GitHub API
4. Posts a success comment on the issue with the PR link

No human is involved at any point.

---

#### HITL_REQUIRED
**Selected when:** any of the following:
- Risk level is MEDIUM or HIGH (rules or Haiku flagged the fix as dangerous)
- Severity is CRITICAL (always requires human sign-off, even on LOW risk fixes)

**What happens:**
1. Posts a comment to the issue containing severity, confidence, risk level, risk reasons, and the full diff (up to 3000 chars), with instructions to reply `/approve` or `/reject`
2. `wait_for_approval()` polls GitHub comments every 60 seconds for up to 30 minutes
3. `/approve` → proceeds to commit + push + PR (same as AUTO_MERGE above)
4. `/reject` → deletes the fix branch, posts a rejection acknowledgement
5. Timeout → deletes the fix branch, posts a timeout notice

> **Phase 2:** HITL approval moves to Slack. The agent posts the diff and approval request to the `#bug-triage` thread for that issue. A human replies `/approve` or `/reject` in the thread — no need to touch GitHub comments. `wait_for_approval()` switches automatically when `SLACK_APP_TOKEN` is set.

---

## Step 5 — Status Notifications

Throughout the pipeline, every major transition is reported via `_notify()` / `_notify_thread()` in the orchestrator:

| Moment | Message |
|--------|---------|
| Issue received | `📥 Issue #N received: <title> — starting triage` |
| Triage complete | `🔬 Triage complete — Severity: HIGH \| Confidence: 92%` |
| Starting fix | `🔧 Starting automated fix for #N...` |
| PR opened | `✅ PR opened: https://github.com/.../pull/8` |
| Fix aborted/rejected | `⚠️ Fix aborted or rejected. Decision: HITL_REQUIRED` |
| Low severity | `🏷️ MEDIUM severity — labelled, no auto-fix` |

**Phase 1:** These print to stdout.  
**Phase 2 (Slack configured):** Posted to `#bug-triage` Slack channel. First message opens a thread; all subsequent messages for the same issue reply in that thread.

---

## Data Flow Summary

```
GitHub Issue
    │
    ▼
webhook_server.py          validates HMAC, dispatches thread
    │
    ▼
orchestrator.py            fetch issue → build BugContext
    │
    ▼
triage.py
  ├─ Haiku              parse reproduction steps from issue body
  ├─ Haiku (tool loop)  run shell/file tools to reproduce the bug
  ├─ Haiku (tool loop)  check docs/source → is this expected behaviour?
  │     └─ not_a_bug = true ──▶ post "not a bug" comment + label, stop
  ├─ Haiku (tool loop)  root cause → {root_cause, confidence, files, buggy_pattern}
  ├─ git + GitHub API   blame_author (pickaxe) + area_experts (commit counts)
  └─ Haiku              classify severity → CRITICAL/HIGH/MEDIUM/LOW
    │
    ▼
solve.py
  ├─ git checkout -b fix/issue-N
  ├─ Haiku (tool loop)             read files → write fix → run tests
  ├─ git diff                       capture proposed_diff
  ├─ evaluate_risk()                LOW / MEDIUM / HIGH / ESCALATE
  └─ Solution Decision              AUTO_MERGE / HITL_REQUIRED / ESCALATE_ONLY
    │
    ├─ ESCALATE_ONLY  ──▶ post escalation comment, stop
    │
    ├─ AUTO_MERGE     ──▶ git commit + push → create PR → post success comment
    │
    └─ HITL_REQUIRED  ──▶ post diff comment → poll for /approve or /reject
                              │
                              ├─ /approve  ──▶ git commit + push → create PR
                              ├─ /reject   ──▶ delete branch, acknowledge
                              └─ timeout   ──▶ delete branch, notify
```

---

## Key Files

| File | Responsibility |
|------|---------------|
| `agent/main.py` | CLI entry: `--issue N` or `--serve` |
| `agent/webhook_server.py` | FastAPI on :9090, HMAC validation, thread dispatch |
| `agent/orchestrator.py` | Pipeline coordinator, severity routing, status notifications |
| `agent/triage.py` | Haiku parse → Haiku reproduce → Haiku root cause → Haiku classify |
| `agent/solve.py` | Haiku fix loop, diff capture, autonomy routing, PR/HITL/escalate |
| `agent/autonomy.py` | `evaluate_risk()` + `evaluate_autonomy()` — pure functions |
| `agent/hitl.py` | Poll GitHub comments for `/approve` or `/reject` |
| `agent/models.py` | `BugContext`, `Severity`, `RiskLevel`, `AutonomyDecision` |
| `agent/tools/github_tools.py` | `GitHubClient` — issue fetch, comment post, label, PR create, commit author lookup, file top authors |
| `agent/tools/shell_tools.py` | `run_shell()`, `git_diff()`, `git_find_introducer_sha()` |
| `agent/tools/file_tools.py` | `read_file()`, `write_file()` |

---

---

## Demo Flow (End-to-End Walkthrough)

This is the exact sequence to run for a live demo, using the two pre-introduced bugs.

### Prerequisites

All of the following must be running before you open an issue:

```bash
# Terminal 1 — Vikunja backend
cd /Users/kadishay/Code/vikunja
./vikunja                         # listens on :3456

# Terminal 2 — Agent webhook server
cd /Users/kadishay/Code/port
python -m agent.main --serve      # listens on :9090

# Terminal 3 — ngrok tunnel (maps GitHub → localhost)
ngrok http 9090                   # copy the https://xxx.ngrok-free.dev URL
```

GitHub webhook must be configured at `kadishay/vikunja → Settings → Webhooks`:
- URL: `https://<ngrok-id>.ngrok-free.dev/webhook`
- Secret: `bugtriage2026`
- Events: Issues only

---

### Demo Bug 1 — Backend (Go): Reminders Fire for Completed Tasks

**What the bug does:**  
`pkg/models/task_overdue_reminder.go` line 43 uses `And("done = true")` instead of `And("done = false")`. The overdue reminder cron sends emails for tasks already completed and silently skips genuinely overdue pending tasks.

**Step 1 — Introduce the bug:**
```bash
bash bugs/introduce_bugs.sh
# commits and pushes to kadishay/vikunja with a plausible developer message
# no rebuild needed — the agent reads source files directly, not the running binary
```

**Step 2 — Open a GitHub issue on `kadishay/vikunja`:**

Title:
```
Getting overdue reminder emails for tasks I've already completed
```

Body:
```
## Description
I keep receiving overdue reminder emails for tasks that I've already marked as done.
At the same time, tasks that are genuinely overdue and still open don't seem to be
triggering any reminders.

## Reproduction
1. Mark a task as done
2. Wait for the overdue reminder job to run
3. Expected: no reminder for completed tasks; reminders for pending overdue tasks
4. Actual: reminder arrives for the completed task; pending overdue tasks are silent
```

**What the agent does (automatically, ~2–3 minutes):**

1. Webhook fires → agent receives the issue
2. **Triage:**
   - Haiku extracts reproduction steps from the issue body
   - Haiku searches by filename: `find pkg/models -name '*reminder*'` → `task_overdue_reminder.go`
   - Haiku reads the file, identifies `And("done = true")` as the bug
   - Haiku classifies: **HIGH** severity
   - Triage comment posted to GitHub issue
3. **Solve:**
   - Haiku checks git history to confirm original value was `done = false`
   - Writes the 1-line fix, commits immediately to `fix/issue-N`
   - Risk evaluated: 1 file, 1 line, confidence ≥ 0.85 → **LOW risk**
   - Decision: **AUTO_MERGE**
   - Pushes branch, opens PR, posts success comment with PR link

**To reset:**
```bash
bash bugs/revert_bugs.sh
# reverts source, commits, and pushes to kadishay/vikunja
```

---

### Demo Bug 2 — Frontend (Vue 3): Done Bucket Condition Inverted

**What the bug does:**  
`frontend/src/stores/kanban.ts:173` has `=== currentView.doneBucketId` instead of `!== currentView.doneBucketId`. Marking a task as done in Kanban view doesn't move it to the Done column.

**Step 1 — Introduce the bug:**
```bash
bash bugs/introduce_bugs.sh
# commits and pushes both bugs together — run once before either demo
```

**Step 2 — Open a GitHub issue on `kadishay/vikunja`:**

Title:
```
Bug: Marking a task as done in Kanban view does not move it to the Done bucket
```

Body:
```
##Description
In Kanban view, checking a task as done leaves it in its current bucket
instead of moving it to the configured Done bucket.

##Reproduction
Open Kanban view
Create a task in any non-done bucket
Check the task as done
Expected: task moves to the Done column
Actual: task stays in its original bucket
```

**What the agent does (automatically, ~2–3 minutes):**

1. Webhook fires → agent receives the issue
2. **Triage:**
   - Haiku extracts reproduction steps (manual UI steps)
   - Haiku reads `kanban.ts` to inspect the condition
   - Haiku identifies root cause: `=== doneBucketId` should be `!== doneBucketId` (inverted condition is a no-op), confidence ~0.90+
   - Haiku classifies: **HIGH** severity
   - Triage comment posted to GitHub issue
3. **Solve:**
   - Haiku reads `kanban.ts` and writes the fix (flips `===` to `!==`)
   - Risk evaluated: 1 file, 1 line, confidence ≥ 0.85 → **LOW risk**
   - Decision: **AUTO_MERGE**
   - Commits to `fix/issue-N`, pushes, opens PR
   - Success comment posted to issue with PR link

**To reset:**
```bash
bash bugs/revert_bugs.sh
# reverts source, commits, and pushes to kadishay/vikunja
```

---

### Demo: HITL Path (optional)

To demonstrate the human-in-the-loop flow, temporarily lower the confidence threshold or edit `autonomy.py` to force `HITL_REQUIRED`. The agent will:

1. Post the proposed diff as a GitHub comment on the issue
2. Wait up to 30 minutes for a reply
3. Reply `/approve` → agent commits + opens PR
4. Reply `/reject` → agent deletes the branch + acknowledges

---

### What to Observe During the Demo

| Moment | Where to look |
|--------|--------------|
| Issue opened | GitHub issue page |
| Triage comment appears | GitHub issue → comments (within ~60–90s) |
| Pipeline logs | Terminal running `--serve` (stdout) |
| Fix branch created | `kadishay/vikunja` → branches |
| PR opened | `kadishay/vikunja` → pull requests |
| Success comment | GitHub issue → final comment with PR link |

---

## Environment Variables Required

```
ANTHROPIC_API_KEY        Anthropic API access
GITHUB_TOKEN             GitHub PAT with repo scope (read + write issues/PRs)
GITHUB_REPO              owner/repo of the Vikunja fork (kadishay/vikunja)
GITHUB_WEBHOOK_SECRET    HMAC secret configured in GitHub webhook settings
VIKUNJA_REPO_PATH        Local path to the Vikunja clone (/Users/kadishay/Code/vikunja)
VIKUNJA_API_BASE         http://localhost:3456
VIKUNJA_API_TOKEN        Vikunja user token for API calls during reproduction
```
