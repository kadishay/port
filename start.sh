#!/usr/bin/env bash
#
# start.sh — Railway container entrypoint.
#
# Coordinates three long-running processes inside one container so they can
# share localhost networking and a filesystem (agent/solve.py and
# agent/triage.py mutate the Vikunja git working tree directly and have
# literal http://localhost:3456 / :4173 strings baked into their prompts —
# see docs/superpowers/plans/2026-08-20-remote-deployment.md):
#
#   1. Vikunja backend (Go)     -> :3456, backgrounded
#   2. Vikunja frontend (Vite)  -> :4173, backgrounded
#   3. Agent webhook server     -> :9090, foreground (keeps the container alive)
#
# $VIKUNJA_REPO_PATH points at a Railway-mounted persistent Volume, which may
# be empty (first deploy) or already populated, possibly with an in-progress
# fix/issue-N branch (redeploy) — see Step 1 below.
#
# NOTE (precondition, not automated here): before the *first* deploy, the
# local vikunja.db (and files/vikunja.db) must be copied onto the volume at
# the path config.yml expects, so project 3 / view 20 / buckets 13-15 match
# what the agent's prompts hardcode. See the Task 3 plan step for why this
# is a one-time manual upload rather than something scripted here.

set -euo pipefail

# set -e is a deliberate addition beyond the plan's literal text: without it,
# a failed `git clone` or `mage build` would silently fall through to later
# steps (e.g. starting a stale/missing binary) instead of failing the
# container boot loudly. Flagging this per the task's YAGNI self-review
# instruction rather than adding it silently.

: "${VIKUNJA_REPO_PATH:?VIKUNJA_REPO_PATH must be set}"
: "${GITHUB_REPO:?GITHUB_REPO must be set (e.g. kadishay/vikunja)}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN must be set}"

log() {
  echo "[start.sh] $*"
}

# --- Step 1: First-run repo bootstrap ---------------------------------------
# Presence of a .git directory (not just "directory is non-empty") is what
# distinguishes "first boot, needs clone" from "already populated, don't
# touch" — an empty-but-mounted volume dir is not itself a signal, and an
# already-cloned repo must never be force-reset (a redeploy shouldn't
# discard an in-progress fix/issue-N branch from a run interrupted by a
# previous redeploy).
#
# Plain `git clone` doesn't work here: the volume may already have
# non-repo files on it (e.g. a pre-placed vikunja.db/config.yml from the
# Step 2 seed-data upload) before the repo is ever cloned, and `git clone`
# refuses to clone into a non-empty directory. Instead: `git init` +
# `remote add` + `fetch` + `checkout -f` into the existing directory,
# which works whether it's empty or has stray non-git files sitting in it.
if [ ! -d "$VIKUNJA_REPO_PATH/.git" ]; then
  log "Step 1: $VIKUNJA_REPO_PATH has no .git — bootstrapping ${GITHUB_REPO} in place (first boot)"
  mkdir -p "$VIKUNJA_REPO_PATH"
  git -C "$VIKUNJA_REPO_PATH" init
  git -C "$VIKUNJA_REPO_PATH" remote add origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"
  git -C "$VIKUNJA_REPO_PATH" fetch origin
  git -C "$VIKUNJA_REPO_PATH" checkout -f main
else
  # Already cloned (redeploy). Never force-reset — that would discard an
  # in-progress fix/issue-N branch from a run interrupted by a previous
  # redeploy. Only advance main when it's safe to do so: working tree
  # clean AND currently on main. Otherwise leave it alone entirely (an
  # in-progress run owns the working tree right now).
  current_branch="$(git -C "$VIKUNJA_REPO_PATH" rev-parse --abbrev-ref HEAD)"
  if [ -z "$(git -C "$VIKUNJA_REPO_PATH" status --porcelain)" ] && [ "$current_branch" = "main" ]; then
    log "Step 1: $VIKUNJA_REPO_PATH is clean on main — fetching and fast-forwarding (redeploy)"
    git -C "$VIKUNJA_REPO_PATH" fetch origin
    git -C "$VIKUNJA_REPO_PATH" merge --ff-only origin/main
  else
    log "Step 1: $VIKUNJA_REPO_PATH is on '${current_branch}' (dirty or non-main) — leaving working tree as-is (in-progress run)"
  fi
fi

# --- Step 2: Seed data migration ---------------------------------------------
# NOT automated here — see the header note above. This is a one-time manual
# step performed against the Railway volume before the first deploy.

# --- Step 3: Configure git push credentials at runtime -----------------------
# Runs on every container start (idempotent — remote set-url is safe to
# re-run). The token lives only in this running container's on-volume git
# config, never in a committed file or image layer.
log "Step 3: configuring git push credentials for ${GITHUB_REPO}"
git -C "$VIKUNJA_REPO_PATH" remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/${GITHUB_REPO}.git"

# --- Step 4: Build and start Vikunja backend ---------------------------------
# Build blocks (want a fresh binary before launching it); the binary itself
# is backgrounded so this script can move on to the frontend and, later,
# exec the agent. No skip-if-unchanged optimization — the brief marks that
# optional, and rebuilding every start is simpler and correct.
log "Step 4: building Vikunja backend"
(cd "$VIKUNJA_REPO_PATH" && mage build)

log "Step 4: starting Vikunja backend"
(cd "$VIKUNJA_REPO_PATH" && ./vikunja) 2>&1 | sed -u 's/^/[vikunja-be] /' &

# --- Step 5: Start Vikunja frontend -------------------------------------------
# pnpm dev (not a production build) to preserve exact parity with the
# current local demo behavior; kept as a follow-up optimization per the
# brief, not required for a first working deploy.
#
# `pnpm install` runs synchronously (blocks), same pattern as Step 4's
# `mage build` — otherwise a cold install would run concurrently with the
# Step 6 readiness wait and silently eat into its timeout budget. Only the
# `pnpm dev` launch itself is backgrounded.
log "Step 5: installing Vikunja frontend dependencies"
(cd "$VIKUNJA_REPO_PATH/frontend" && pnpm install)

log "Step 5: starting Vikunja frontend"
(cd "$VIKUNJA_REPO_PATH/frontend" && pnpm dev) 2>&1 | sed -u 's/^/[vikunja-fe] /' &

# --- Step 6: Wait for both to be reachable ------------------------------------
# Plain `curl -s -o /dev/null` (no -f): success just means "got a TCP
# connection and an HTTP response", regardless of status code, since a 404
# on "/" still proves the process is up and listening. A hard timeout
# prevents looping forever if a process never comes up — the script exits
# non-zero instead, so Railway sees a failed boot and can restart/report it.
wait_for() {
  local url="$1" name="$2" timeout_s="${3:-180}" interval_s=3 waited=0
  log "Step 6: waiting for $name at $url (timeout ${timeout_s}s)"
  until curl -s -o /dev/null "$url"; do
    waited=$((waited + interval_s))
    if [ "$waited" -ge "$timeout_s" ]; then
      log "ERROR: $name did not become reachable at $url within ${timeout_s}s"
      return 1
    fi
    sleep "$interval_s"
  done
  log "Step 6: $name is reachable at $url (after ~${waited}s)"
}

wait_for "http://localhost:3456" "vikunja-be"
wait_for "http://localhost:4173" "vikunja-fe"

# --- Step 7: Start the agent in the foreground --------------------------------
# exec replaces this script's process (PID 1's child) with the agent, so it
# receives signals directly and its exit is what Railway sees as the
# container exiting (triggering a restart per Railway's policy).
log "Step 7: starting agent webhook server (foreground)"
exec python3 -m agent.main --serve
