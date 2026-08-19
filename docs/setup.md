# Initial Setup

## 1. Clone repos

```bash
git clone https://github.com/kadishay/vikunja /Users/kadishay/Code/vikunja
git clone https://github.com/kadishay/port /Users/kadishay/Code/port
```

## 2. Configure environment

```bash
cd /Users/kadishay/Code/port
cp .env.sample .env
```

Fill in `.env`:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `GITHUB_TOKEN` | `github_pat_...` |
| `GITHUB_REPO` | `kadishay/vikunja` |
| `GITHUB_WEBHOOK_SECRET` | any secret string |
| `VIKUNJA_REPO_PATH` | `/Users/kadishay/Code/vikunja` |
| `VIKUNJA_API_BASE` | `http://localhost:3456` |
| `VIKUNJA_API_TOKEN` | Vikunja agent user token (see step 4) |
| `VIKUNJA_USERNAME` | `agent` |
| `VIKUNJA_PASSWORD` | agent user password |
| `SLACK_BOT_TOKEN` | `xoxb-...` (Phase 2 only) |
| `SLACK_APP_TOKEN` | `xapp-...` (Phase 2 only) |
| `SLACK_CHANNEL` | `#bug-triage` (Phase 2 only) |

## 3. Install agent dependencies

```bash
cd /Users/kadishay/Code/port
pip install -r requirements.txt
```

## 4. Start Vikunja

**Backend** (port 3456):
```bash
cd /Users/kadishay/Code/vikunja
mage build
./vikunja
```

**Frontend** (port 4173):
```bash
cd /Users/kadishay/Code/vikunja/frontend
pnpm install
pnpm dev
```

Then:
1. Open `http://localhost:4173` and create an **agent** user
2. Log in as that user → Settings → API Tokens → create one
3. Paste the token into `.env` as `VIKUNJA_API_TOKEN`
4. Create a **Kanban project** with a Done bucket — the FE demo expects it at `/projects/3/20`. If the IDs differ, update `kanban_hint` in `agent/triage.py` and `kanban_verify` in `agent/solve.py`

## 5. Apply the demo bugs

```bash
cd /Users/kadishay/Code/port
bash bugs/introduce_bugs.sh
```

Revert with `bash bugs/revert_bugs.sh` (use between demo runs).

## 6. Verify with tests

```bash
# Unit tests only (fast, <15s)
python -m pytest tests/ --ignore=tests/test_demo_bugs_integration.py -q

# Full integration tests (real LLM, ~2.5 min)
python -m pytest tests/test_demo_bugs_integration.py -v --timeout=300
```

## 7. Start agent server + expose via ngrok

```bash
# Terminal 1 — agent webhook server
python -m agent.main --serve   # listens on :9090

# Terminal 2 — ngrok tunnel
ngrok http 9090
```

Configure GitHub webhook:
- Repo **Settings → Webhooks → Add webhook**
- URL: `https://<ngrok-id>.ngrok.io/webhook`
- Secret: matches `GITHUB_WEBHOOK_SECRET` in `.env`
- Content type: `application/json`
- Event: **Issues**

## 8. Run the demo

```bash
# 1. Bugs are already applied (step 5)
# 2. Server and ngrok are running (step 7)
# 3. Open a GitHub issue with title matching one of the bugs, e.g.:
#    "Bug: Overdue reminder fires for completed tasks"
#    "Bug: Marking a task as done in Kanban view doesn't move it to Done column"
# Agent posts a triage comment and opens a fix PR within ~3 minutes
```

## Manual trigger (no webhook needed)

```bash
python -m agent.main --issue <github-issue-number>
```
