# Bug Triage & Solve Agent — Project Context

## Project Overview

**Assignment:** 48-hour technical project to build an agentic workflow that triages and solves bugs in a GitHub repository.

**Deliverables:**
- Working agent deployed locally, GitHub repo with code
- 5–10 minute video walkthrough
- 1–2 page write-up
- Two pre-introduced bugs (1 backend Go, 1 frontend Vue 3) in Vikunja fork
- Budget: $50 Anthropic API credits

**Status:** Design + implementation plan complete. Awaiting code implementation.

---

## Key Files & Documents

- **Design Spec:** `docs/superpowers/specs/2026-08-18-bug-triage-agent-design.md`
  - Phase 1 (MVP): local agent, GitHub webhook, GitHub-based HITL
  - Phase 2 (in scope): Slack integration for status updates & bidirectional HITL
  - Phase 3 (optional): production deployment, codebase memory/cross-bug learning
  - Autonomy decision matrix: rules for auto-merge vs. HITL vs. escalate-only

- **Implementation Plan:** `docs/superpowers/plans/2026-08-18-bug-triage-agent.md`
  - 10 tasks, bite-sized with exact code
  - Task-by-task breakdown: scaffolding → tools → agents → webhook → Slack

---

## Architecture at a Glance

```
GitHub Issue (opened)
       ↓
Webhook (port 9090) ← ngrok tunnel
       ↓
Orchestrator (Python)
       ├─ Triage Agent (Haiku parse → Opus root cause → Haiku classify)
       ├─ Autonomy Check (rules: severity, files, lines, confidence, keywords)
       └─ Solve Agent (Opus fix proposal → auto-PR OR HITL)
            ├─ AUTO_PR: apply patch → tests → PR (no human needed)
            └─ HITL_REQUIRED: post diff → wait for /approve or /reject
                 (auth/migration/security-sensitive diffs always land here — never auto-merged, but still get a suggested fix)
```

**Models used:**
- Haiku 4.5 ($1/$5 per MTok): all steps — parsing, reproduction, root cause (with read_file, capped at 5 iterations), classification, fix proposal, HITL polling
- Opus 4.8 ($5/$25 per MTok): fallback only — one retry when Haiku's confidence is < 0.70 on root cause analysis (triage 2d) or backend curl verification (solve 4b²). Haiku alone is sufficient for single-file logic bugs at 5× lower cost and ~5× faster; Opus only kicks in on the minority of runs where Haiku hedges.

**Cost per bug:** ~$0.05–0.10 total

---

## The Two Demo Bugs

Both bugs have exact file locations and diffs captured in the implementation plan.

### Bug 1: Backend (Go) — Reminders Fire for Completed Tasks

**File:** `/Users/kadishay/Code/vikunja/pkg/models/task_overdue_reminder.go:43`

**Change:**
```go
// Buggy: queries completed tasks instead of pending ones
And("done = true")

// Fixed:
And("done = false")
```

**Effect:** The overdue reminder cron (runs every minute) sends emails for tasks the user has already completed, while genuinely overdue pending tasks receive no reminder at all.

**Severity:** HIGH (wrong data shown, core feature broken)

### Bug 2: Frontend (Vue 3) — Done Bucket Condition Inverted

**File:** `/Users/kadishay/Code/vikunja/frontend/src/stores/kanban.ts:173`

**Change:**
```typescript
// Buggy: condition inverted
if (task.done && currentView.doneBucketId !== 0 && currentTaskBucket.id === currentView.doneBucketId) {

// Fixed:
if (task.done && currentView.doneBucketId !== 0 && currentTaskBucket.id !== currentView.doneBucketId) {
```

**Effect:** Marking a task as done in Kanban view doesn't move it to the Done column (condition only fires when task is already in done bucket — a no-op).

**Reproduction:**
```
1. Open Kanban view
2. Create a task in a non-done bucket
3. Check task as done
4. Expected: task moves to Done column
5. Actual: task stays in current bucket
```

**Severity:** HIGH (core feature broken)

---

## Autonomy Decision Matrix

The agent opens the PR automatically (no pre-approval gate) if ALL criteria are met; otherwise requires HITL. Either way a human still merges the PR on GitHub — see `docs/flow.md`.

| Criteria | Threshold | Notes |
|----------|-----------|-------|
| Severity | HIGH or lower | Never skip HITL for CRITICAL (alert human but attempt fix) |
| Files changed | ≤ 2 files | Both demo bugs touch 1 file |
| Lines changed | ≤ 30 lines | Both demo bugs are 1-line fixes |
| Tests | All existing pass, no test files modified | Run backend tests + frontend unit tests |
| Model confidence | ≥ 0.85 | Opus outputs structured confidence score |
| Fix type | Pure correction of existing logic | No new behavior, no new dependencies |

**Always HITL, never auto-merged (fix is still suggested):**
- Database schema changes (migration files)
- Auth, permissions, session token handling
- API contract changes (route signatures, response shapes)

These are detected via a keyword match on the diff plus a Haiku semantic pass (which can flag e.g. password-verification code even when no keyword matches literally). Either signal forces `HITL_REQUIRED`, overriding whatever the size/confidence rules would otherwise say — a human must approve before the fix merges, but the agent still writes and proposes it. There is no longer an "escalate with no fix attempt" path.

Both demo bugs meet auto-PR criteria.

---

## Project Structure

```
port/
├── agent/
│   ├── __init__.py
│   ├── main.py                # CLI: --issue N or --serve
│   ├── models.py              # BugContext, Severity, AutonomyDecision enums
│   ├── orchestrator.py        # Triage → autonomy → solve pipeline
│   ├── triage.py              # Triage agent (agentic loop with tool runner)
│   ├── solve.py               # Solve agent (manual loop for HITL gates)
│   ├── autonomy.py            # Autonomy decision rules (pure functions)
│   ├── hitl.py                # HITL interface (GitHub phase 1, Slack phase 2)
│   ├── webhook_server.py      # FastAPI app, port 9090
│   ├── slack_client.py        # Slack integration (Phase 2)
│   └── tools/
│       ├── __init__.py
│       ├── github_tools.py    # GitHubClient (REST API wrappers)
│       ├── shell_tools.py     # run_shell, git_diff, git operations
│       └── file_tools.py      # read_file, write_file
├── bugs/
│   ├── introduce_bugs.sh      # Applies both bugs to the Vikunja fork
│   └── revert_bugs.sh         # Reverts both bugs (for reset between demos)
├── tests/
│   ├── __init__.py
│   ├── test_*.py              # Unit tests for each module
│   └── [add more as implemented]
├── docs/
│   └── superpowers/
│       ├── specs/
│       │   └── 2026-08-18-bug-triage-agent-design.md
│       └── plans/
│           └── 2026-08-18-bug-triage-agent.md
├── requirements.txt
├── .env.sample
├── pytest.ini
├── AI Director Task.pdf        # Original assignment
└── CLAUDE.md                   # This file
```

---

## Environment Setup

**Required environment variables** (copy from `.env.sample`, fill in real values):

```bash
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=github_pat_...              # For GitHub API access
GITHUB_REPO=kadishay/vikunja              # Owner/repo of the fork
GITHUB_WEBHOOK_SECRET=your-secret-here   # For webhook signature validation
VIKUNJA_REPO_PATH=/Users/kadishay/Code/vikunja
VIKUNJA_API_BASE=http://localhost:3456
VIKUNJA_API_TOKEN=<vikunja-token>        # API token for the agent user

# Phase 2 (optional)
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=#bug-triage
```

---

## Vikunja Repository

- Located at: `/Users/kadishay/Code/vikunja`
- Backend: Go, port 3456
- Frontend: Vue 3 + TypeScript, port 4173 (dev)
- Database: SQLite for testing
- Build: `mage build` (backend), `pnpm dev` (frontend)
- Tests: `mage test:feature`, `mage test:web`, `pnpm test:unit`

**Architecture layers:**
- `pkg/models/` — Domain entities + CRUD (where both bugs live)
- `pkg/services/` — Business logic
- `pkg/routes/` — HTTP endpoints
- `frontend/src/stores/` — Pinia state management

---

## Implementation Plan (10 Tasks)

See `docs/superpowers/plans/2026-08-18-bug-triage-agent.md` for full detail.

1. **Scaffolding + BugContext model** — pytest, requirements, dataclass
2. **Introduce bugs** — shell scripts to apply/revert bugs in Vikunja fork
3. **Tool layer** — shell, file, git operations
4. **GitHub API client** — GitHubClient with issue/PR/comment methods
5. **Autonomy module** — Decision rules (pure functions, unit-testable)
6. **Triage agent** — Haiku parse → Opus root cause → Haiku classify (agentic loop)
7. **HITL module** — GitHub comment polling (Phase 1)
8. **Solve agent** — Opus fix proposal → autonomy decision → apply → test → PR
9. **Webhook + orchestrator + CLI** — FastAPI server, orchestrator, CLI entry
10. **Phase 2 Slack** — SlackClient, Slack HITL, status updates

---

## Phase 1 (MVP) Flow

### Manual trigger (for testing):
```bash
python -m agent.main --issue 42
```

### Webhook trigger (production):
```bash
# 1. Apply bugs to Vikunja
bash bugs/introduce_bugs.sh

# 2. Start ngrok
ngrok http 9090

# 3. Configure GitHub webhook
# Settings → Webhooks → Add webhook
# URL: https://<ngrok-id>.ngrok.io/webhook
# Secret: matches .env GITHUB_WEBHOOK_SECRET
# Events: Issues

# 4. Start agent server
python -m agent.main --serve

# 5. Open issue on GitHub
# Title: "Bug: tasks due in the next 38h incorrectly flagged as overdue"
# Body: [see plan for reproduction steps]

# 6. Agent posts triage comment, then fix PR within ~3 minutes
```

---

## Phase 2 (Slack) Flow

Same as Phase 1, but:
- Agent posts status updates to `#bug-triage` Slack channel at every step
- HITL approval happens in Slack thread (reply `/approve` or `/reject`)
- No need to touch GitHub comments for human interaction

---

## Phase 3 (Optional) — Production Deployment + Memory

### Deployment
- Move Vikunja + agent to cloud (Fly.io / Railway / Docker Compose on VPS)
- Agent runs as a service, not on a laptop
- GitHub webhook points directly to cloud host IP

### Codebase Memory & Cross-Bug Learning
- Static: index Vikunja codebase into vector DB on first run
- Dynamic: after each resolved bug, save `Resolution` record (pattern, root cause, files, fix pattern)
- On new bugs: retrieve similar past resolutions as few-shot context for Opus
- Improves token efficiency, speed, and confidence scores over time

---

## Key Decisions & Rationale

### SDK Choice: Anthropic SDK (not LangChain)
- Direct cost control and visibility (critical for $50 budget)
- Haiku for cheap classification/reproduction, Opus/thinking only when needed
- Tool runner (beta) for triage, manual loop for solve (HITL gates need fine control)
- LangChain would add abstraction overhead and indirect cost tracking

### Port 9090 (not 8080/3000)
- Avoids conflicts: Vikunja :3456, frontend :4173, common dev :8080/3000

### Autonomy Matrix Over Naive Rules
- Explicit thresholds make agent behavior predictable and debuggable
- Severity escalates properly (CRITICAL still attempts fix but never auto-merges)
- Can be tuned post-demo if needed

### Two Tool Strategies
- **Triage:** Tool runner (automatic loop, simple parse + reproduce flow)
- **Solve:** Manual loop (need to pause at autonomy decision + HITL gates)

### HITL Phase 1 → Phase 2 Migration
- Phase 1 polls GitHub comments (`wait_for_approval` in hitl.py)
- Phase 2 replaces with Slack thread listener (same interface, swappable)
- Both Phase 1 and Phase 2 can coexist (determined by env var `SLACK_APP_TOKEN`)

---

## Testing Strategy

- Unit tests for autonomy module (pure functions, easy to mock)
- Mocked GitHub API tests (responses library)
- No integration tests (too slow; validated manually during 48-hour deadline)
- All tests in `tests/` directory, run with `pytest`

---

## Cost Tracking

**Per bug run (triage + solve), measured:** ~$0.13 backend / ~$0.18 frontend (no Playwright) / ~$0.23 frontend (Playwright on) — real numbers from `tests/test_demo_bugs_integration.py` runs, see `docs/flow.md`'s "Recent execution averages" section for methodology. These are Haiku-only runs; add the Opus fallback cost (rare — only fires when confidence < 0.70 on root cause or backend verify) on top for runs that hit it.

**Budget headroom:** $50 / ~$0.20 ≈ 250 bug runs. Plenty for demo + iteration.

---

## Commit Strategy

Each task gets its own commit with conventional commit message:
```
feat: add triage agent (Haiku reproduce → Opus root cause → Haiku classify)
feat: add autonomy decision module
feat: add Slack integration for Phase 2
```

Main branch only (no branches needed for 48-hour sprint).

---

## Next Steps (When Starting Implementation)

1. Start with Task 1 (scaffolding) — establishes project structure
2. Use subagent-driven-development or executing-plans skill to run tasks sequentially
3. Commit after each task
4. After Task 9: run `python -m agent.main --issue <N>` to smoke-test
5. After Task 10: enable Slack and test end-to-end
6. Record video walkthrough
7. Write 1–2 page summary

---

## Common Commands

```bash
# Install dependencies
cd /Users/kadishay/Code/port && pip install -r requirements.txt

# Run all tests
python -m pytest tests/ -v

# Run specific test
python -m pytest tests/test_autonomy.py -v

# Manual issue processing
python -m agent.main --issue 42

# Start webhook server
python -m agent.main --serve

# Apply bugs to Vikunja fork
bash bugs/introduce_bugs.sh

# Revert bugs
bash bugs/revert_bugs.sh

# View git log
git log --oneline -10

# Push changes
git push origin main
```

---

## References

- Anthropic SDK docs: https://github.com/anthropic-ai/anthropic-sdk-python
- GitHub API docs: https://docs.github.com/en/rest
- Slack SDK docs: https://slack.dev/python-slack-sdk/
- Vikunja docs: https://vikunja.io/docs/

---

## Contact & Attribution

- Assignment: 48-hour technical project (provided as PDF)
- Implemented by: Claude (Anthropic SDK)
- User: Yotam Kadishay (yotam.kadishay@gmail.com)
