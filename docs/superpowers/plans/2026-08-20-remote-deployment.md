# Remote Deployment (Railway) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the full pipeline (Vikunja backend + frontend + the agent webhook server, including Playwright) on Railway's Hobby plan instead of a laptop + ngrok, so the webhook is reachable without the developer's machine being on.

**Why this shape:** Discovered via code inspection (not assumption) that the agent has two hard couplings that rule out a normal multi-service split:
1. `agent/solve.py` and `agent/triage.py` mutate the Vikunja git working tree directly (`git checkout`, `read_file`, `write_file`, `run_shell`) — there is no API-mediated code-fix path, so the agent process and the Vikunja git clone must share a filesystem.
2. Several tool-guidance prompts have literal `http://localhost:3456` / `http://localhost:4173` strings baked in (not just env-var defaults), so the Vikunja backend, frontend, and agent must all be reachable at those same local addresses from one process's point of view.

Net effect: Vikunja backend + frontend + agent all run in **one Railway service**, one container, started together — not Railway's usual one-service-per-component pattern.

**Playwright requirement:** the user needs FE screenshot verification working remotely, which rules out Railway's default Nixpacks builder (no clean apt-equivalent for Chromium's system deps). Decision: **deploy via a single Dockerfile** based on Microsoft's official Playwright image, which bundles Chromium + all its system libraries already. This is a deliberate, scoped exception to "avoid Docker" — one Dockerfile for one Railway service, not a multi-container Compose setup.

**Tech stack (unchanged from local):** Go (Vikunja backend, built via `mage`), Vue 3 + Vite (frontend), Python 3.11 (agent), Playwright/Chromium, SQLite.

---

## Global Constraints

- One Dockerfile, one Railway service. No Docker Compose, no splitting Vikunja/agent across services — the localhost/filesystem coupling above makes that actively harmful, not just unnecessary.
- No changes to agent business logic in this plan except the concurrency lock (Task 1) — this is a deployment/infra plan, not a refactor.
- The container must reach a **working parity state** with the current local demo: same hardcoded `localhost:3456`/`:4173` addresses, same Vikunja project/view/bucket IDs the prompts assume (project 3, view 20, buckets 13/14/15). Achieved by migrating the actual local `vikunja.db` onto the Railway volume rather than seeding a fresh instance — a fresh instance's auto-generated IDs are not guaranteed to match.
- Secrets (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `VIKUNJA_API_TOKEN`) are set as Railway service variables, never baked into the image or committed.
- `GITHUB_TOKEN` is reused for `git push` at container-startup (via a runtime `git remote set-url` using the token) — no separate SSH key needed, never baked into the image layer. (It IS written to disk on the volume, as part of the persisted `.git/config` — acceptable for a single-tenant deployment, but distinct from "never written to disk.")

---

### Task 1: Per-Issue Concurrency Lock

**Why:** `webhook_server.py` spawns an unguarded daemon thread per webhook event, and every run mutates the *same* Vikunja working tree (`git checkout -b fix/issue-N`, etc.). Locally this was a one-issue-at-a-time demo; a real public webhook can receive overlapping/retried deliveries. Without a lock, two concurrent runs racing on one working tree reproduces the exact "bug isn't reproducing" false-alarm class already seen during local testing — but for real, unattended.

**Files:**
- Modify: `agent/orchestrator.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Produces: a module-level `threading.Lock()` in `orchestrator.py` held for the full duration of `run_pipeline()`'s working-tree-mutating section (triage's `run_shell`/`read_file` exploration doesn't need to block, but `solve.py`'s branch-checkout-through-push sequence does at minimum).
- Simplest correct option: wrap the entire `run_pipeline(issue_number)` body in the lock. Serializes all pipeline runs globally (no two issues process concurrently) — acceptable at this scale; a per-repo-path lock would be over-engineering for a single-Vikunja-instance deployment.
- The lock can be held for up to ~30 minutes at a stretch when the in-flight run is parked in `solve.py`'s HITL approval wait — this is intentional (the working tree holds a checked-out `fix/issue-N` branch for the whole wait), not a bug, and is called out with a comment at the lock definition in `orchestrator.py`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`: a test that starts two `run_pipeline()` calls in separate threads (mocking GitHub/Anthropic calls so both would otherwise interleave), and asserts the underlying triage/solve mock calls are never active concurrently (e.g. via a shared counter that asserts `count <= 1` inside the mock, incremented/decremented around the call).

- [ ] **Step 2: Implement**

Add `_pipeline_lock = threading.Lock()` at module scope in `agent/orchestrator.py`; acquire it as the outermost context manager in `run_pipeline()`, wrapping the existing `try/except/finally`.

- [ ] **Step 3: Verify**

Run `pytest tests/test_orchestrator.py -v`. Confirm existing orchestrator tests still pass (lock must not deadlock on single-threaded test calls).

---

### Task 2: Dockerfile

**Why:** Single image containing Go + Node/pnpm + Python + Playwright/Chromium, so Railway can build and run the whole colocated stack from one service definition.

**Files:**
- New: `Dockerfile` (repo root of `port`)
- New: `.dockerignore`

- [ ] **Step 1: Base image**

`FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy` (or latest pinned version compatible with the `playwright` pip package version already in `requirements.txt`) — ships Chromium + all system deps preinstalled, avoiding the Nixpacks friction entirely.

- [ ] **Step 2: Install Go + mage**

Install the Go toolchain (matching Vikunja's `go.mod` version) and `mage` (`go install github.com/magefile/mage@latest`) so the backend can be built from source inside the image.

- [ ] **Step 3: Install Node + pnpm**

Install Node.js (version matching Vikunja frontend's `package.json` engines field) and `pnpm` via corepack.

- [ ] **Step 4: Install Python deps**

`COPY requirements.txt . && pip install -r requirements.txt` for the agent's own dependencies (`playwright` pip package already installs into the base image's existing browser binaries — confirm no re-download needed).

- [ ] **Step 5: Copy agent source**

`COPY agent/ ./agent/` — only the `port` repo's agent code goes into the image. The Vikunja source is **not** baked into the image (see Task 3) — it's cloned into the mounted volume at container startup so the agent's `git push` has a real, writable, credentialed clone.

- [ ] **Step 6: Entrypoint**

`ENTRYPOINT ["/app/start.sh"]` — see Task 3.

- [ ] **Step 7: `.dockerignore`**

Exclude `demo/`, `docs/*.pdf`, `docs/*.png`, `.git/`, `__pycache__/`, `tests/` — keep the image lean.

---

### Task 3: Startup Script

**Why:** three long-running processes (Vikunja backend, Vikunja frontend, agent webhook server) need to start together in the right order, against a persistent volume that may be empty (first deploy) or already populated (redeploy).

**Files:**
- New: `start.sh` (repo root of `port`)

- [ ] **Step 1: First-run repo bootstrap**

If `$VIKUNJA_REPO_PATH` (pointed at the mounted volume, e.g. `/data/vikunja`) doesn't contain a `.git` directory: clone `kadishay/vikunja` into it. If it does: leave it as-is (don't force-reset — a redeploy shouldn't discard an in-progress `fix/issue-N` branch from a run that was interrupted by a redeploy).

- [ ] **Step 2: Seed data migration (one-time, manual precondition — not scripted)**

Document as a precondition, not automated in `start.sh`: before first deploy, copy the local `vikunja.db` (and `files/vikunja.db`) onto the Railway volume at the path `config.yml` expects, so project 3 / view 20 / buckets 13-15 match what the prompts hardcode. Uploading a large binary file into a fresh Railway volume is a one-time manual step (`railway volume` upload or a temporary debug shell), not something to script into `start.sh`.

- [ ] **Step 3: Configure git push credentials at runtime**

```bash
git -C "$VIKUNJA_REPO_PATH" remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/kadishay/vikunja.git"
```
Runs every container start (idempotent) — never written to a committed file, only the in-memory/on-volume git config of the running container.

- [ ] **Step 4: Build and start Vikunja backend**

`mage build` (skip if binary already exists and source hasn't changed — optional optimization, not required for correctness), then launch the binary in the background, logging to stdout with a `[vikunja-be]` prefix (or a separate log file if stdout multiplexing gets noisy).

- [ ] **Step 5: Start Vikunja frontend**

`pnpm install && pnpm dev` in the background, bound to `:4173` — kept as the dev server (not a production build) to preserve exact parity with the current local demo behavior; revisit as a follow-up optimization, not required for first working deploy.

- [ ] **Step 6: Wait for both to be reachable**

Poll `http://localhost:3456` and `http://localhost:4173` (simple curl-in-a-loop with a timeout) before starting the agent, so the first webhook doesn't race a backend that's still booting.

- [ ] **Step 7: Start the agent in the foreground**

`exec python -m agent.main --serve` as the last line — foreground process keeps the container alive; Railway restarts the container if this exits.

---

### Task 4: Railway Service Configuration

**Why:** connects the image/volume/domain/secrets together. Dashboard/CLI steps, not code.

- [ ] **Step 1:** Create the Railway service from this repo, confirm it detects and uses the root `Dockerfile` (not Nixpacks).
- [ ] **Step 2:** Attach a Volume, mounted at the path used for `VIKUNJA_REPO_PATH` (e.g. `/data/vikunja`).
- [ ] **Step 3:** Set service variables: `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, `GITHUB_REPO=kadishay/vikunja`, `GITHUB_WEBHOOK_SECRET`, `VIKUNJA_REPO_PATH=/data/vikunja`, `VIKUNJA_API_BASE=http://localhost:3456`, `VIKUNJA_API_TOKEN`, `PLAYWRIGHT_ENABLED=true`, `PLAYWRIGHT_HEADLESS=true`, `VIKUNJA_USERNAME`, `VIKUNJA_PASSWORD` (required for the agent's own login/token-refresh flow — without them, every FE reproduction silently fails to authenticate), `NOTIFY_USER`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL` (Phase 2 parity), `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (telemetry + dashboard).
- [ ] **Step 4:** Generate Railway's public domain, confirm it serves the agent's `:9090` webhook port (set `PORT`/expose config accordingly if Railway requires it).
- [ ] **Step 5:** Complete the Task 3 Step 2 manual seed-data upload onto the new volume.
- [ ] **Step 6:** First deploy — watch build + boot logs, confirm the Task 3 Step 6 readiness check passes and the agent starts.

**Watch items:** expect the Volume to grow to several GB (the cloned Vikunja repo plus frontend `node_modules`) — request at least 5–10 GB. Expect the built image itself to land around 4 GB (Playwright base image + Go toolchain + Node), which is normal for this single-image approach, not a sign something's wrong.

---

### Task 5: Cut Over the GitHub Webhook

- [ ] **Step 1:** Update `kadishay/vikunja` → Settings → Webhooks → URL to the new Railway domain (`https://<railway-domain>/webhook`), same secret as `GITHUB_WEBHOOK_SECRET`.
- [ ] **Step 2:** Stop the local `ngrok` tunnel and local `python -m agent.main --serve` process — only one webhook target should be live at a time to avoid duplicate pipeline runs on the same issue.
- [ ] **Step 3:** Re-run Demo Bugs 1, 2, and 3 end-to-end against the Railway deployment exactly as documented in `docs/flow.md`'s Demo Flow section, confirming parity with local behavior (including Playwright before/after screenshots).

---

## Explicitly Out of Scope (this plan)

- Parameterizing the hardcoded `localhost`/project-ID assumptions — real fix, but bigger than this deployment task; tracked separately.
- Production-built frontend (`pnpm build` + static serving) instead of `pnpm dev` — optimization, not required for parity.
- Per-repo-path (rather than global) concurrency locking — unnecessary complexity for a single-Vikunja-instance deployment.
