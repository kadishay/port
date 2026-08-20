# Bug Triage & Solve Agent

An agentic pipeline that triages and fixes bugs in a GitHub repository automatically. A GitHub issue triggers a webhook → the agent reproduces the bug, diagnoses the root cause, writes a fix, verifies it, and opens a PR (or requests human approval first, depending on risk).

Built against a [Vikunja](https://vikunja.io/) fork (Go backend + Vue 3 frontend) as the target codebase.

## How it works

```
GitHub Issue (opened/reopened)
       ↓
Webhook → Orchestrator
       ├─ Triage: reproduce → check-reproduced → root cause → severity
       └─ Solve: apply fix → verify → risk check → AUTO_PR or HITL_REQUIRED
```

Full walkthrough with real examples: [`docs/flow.md`](docs/flow.md).

## Running it

**Locally:**
```bash
pip install -r requirements.txt
cp .env.sample .env   # fill in real values
python -m agent.main --issue 42     # process one issue
python -m agent.main --serve        # run the webhook server (port 9090)
```

Full local setup, including the Vikunja fork and demo bugs: [`docs/setup.md`](docs/setup.md).

**Deployed (Railway):** the agent, Vikunja backend, and Vikunja frontend run together in a single container — see [`Dockerfile`](Dockerfile), [`start.sh`](start.sh), and [`docs/superpowers/plans/2026-08-20-remote-deployment.md`](docs/superpowers/plans/2026-08-20-remote-deployment.md) for the full deployment plan and rationale.

## Project layout

| Path | What's there |
|---|---|
| `agent/` | The pipeline itself — orchestrator, triage, solve, autonomy rules, GitHub/Slack/telemetry integrations |
| `bugs/` | Scripts to introduce/revert the two pre-seeded demo bugs in the Vikunja fork |
| `tests/` | Unit tests + real-LLM integration tests for the demo bugs |
| `docs/flow.md` | Step-by-step system walkthrough with real log output |
| `docs/setup.md` | Local environment setup |
| `docs/monitoring.md` | Telemetry + the Supabase-backed dashboard (`dashboard.html`) |
| `docs/superpowers/plans/` & `docs/superpowers/specs/` | Design docs and implementation plans |

## Cost

Haiku 4.5 for most steps, with an Opus 4.8 fallback only on low-confidence root-cause/verification calls. ~$0.10–0.25 per bug run — see `docs/flow.md`'s "Recent execution averages" section for real measured numbers.
