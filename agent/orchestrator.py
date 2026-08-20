import os
import threading
from dotenv import load_dotenv
load_dotenv(override=True)

from agent.models import BugContext
from agent.tools.github_tools import GitHubClient
from agent.triage import run_triage
from agent.solve import run_solve
from agent.cost_tracker import CostTracker
import time
from agent.telemetry import record_run


def _refresh_vikunja_token() -> None:
    import json, subprocess
    username = os.environ.get("VIKUNJA_USERNAME", "")
    password = os.environ.get("VIKUNJA_PASSWORD", "")
    api_base = os.environ.get("VIKUNJA_API_BASE", "http://localhost:3456")
    if not username or not password:
        return
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", f"{api_base}/api/v1/login",
             "-H", "Content-Type: application/json",
             "-d", json.dumps({"username": username, "password": password})],
            capture_output=True, text=True, timeout=10,
        )
        token = json.loads(result.stdout).get("token", "")
        if token:
            os.environ["VIKUNJA_API_TOKEN"] = token
            print("[token] Refreshed Vikunja API token", flush=True)
        else:
            print(f"[token] Refresh failed: {result.stdout[:200]}", flush=True)
    except Exception as e:
        print(f"[token] Failed to refresh token: {e}", flush=True)


# Serializes all pipeline runs globally: two overlapping/retried webhook deliveries
# must never mutate the shared Vikunja working tree (git checkout/commit/push) at the
# same time. Deliberately a single global lock, not per-repo-path — this deployment
# only ever drives one Vikunja working tree.
#
# NOTE: this lock can be held for up to ~30 minutes at a stretch when the
# in-flight run is parked in solve.py's HITL approval wait — that's
# intentional (the working tree holds a checked-out fix/issue-N branch for
# the whole wait, so a second run must not touch it), not a bug. See the
# non-blocking-acquire-then-block pattern below for the observability this
# implies.
_pipeline_lock = threading.Lock()


def run_pipeline(issue_number: int) -> BugContext | None:
    if not _pipeline_lock.acquire(blocking=False):
        print(f"[orchestrator] #{issue_number} — queued behind an in-flight pipeline run", flush=True)
        _pipeline_lock.acquire()  # now block until it's free
    try:
        load_dotenv(override=True)  # Re-read .env on every run so live server picks up changes
        _refresh_vikunja_token()
        gh = GitHubClient()
        issue = gh.get_issue(issue_number)

        fix_branch = f"fix/issue-{issue_number}"
        existing_pr = gh.get_open_pr_for_branch(fix_branch)
        if existing_pr:
            msg = f"ℹ️ Issue #{issue_number} already has an open PR: {existing_pr['html_url']} — skipping (no re-triage/re-solve)."
            print(f"[orchestrator] #{issue_number} — {msg}", flush=True)
            gh.post_comment(issue_number, msg)
            return None

        ctx = BugContext(
            issue_number=issue_number,
            issue_title=issue["title"],
            issue_body=issue.get("body") or "",
            repo_path=os.environ.get("VIKUNJA_REPO_PATH", "/Users/kadishay/Code/vikunja"),
        )

        tracker = CostTracker()
        start = time.time()
        thread_ts = _notify(f"📥 Issue #{issue_number} received: *{issue['title']}* — starting triage")
        ctx.slack_thread_ts = thread_ts or ""

        crashed = False
        try:
            ctx = run_triage(ctx, gh, tracker)

            if ctx.not_a_bug:
                _notify_thread(ctx, f"🚫 Issue #{issue_number} closed as not a bug: {ctx.not_a_bug_reason}")
                return ctx

            if ctx.unable_to_reproduce:
                status = "closed automatically" if ctx.issue_closed else "left open for human review"
                _notify_thread(ctx, (
                    f"🔍 Issue #{issue_number} could not be reproduced "
                    f"(confidence {ctx.reproduction_confidence:.0%}) — {status}."
                ))
                return ctx

            people_tags: list[str] = []
            if ctx.blame_author:
                people_tags.append(f"@{ctx.blame_author} (introduced)")
            people_tags.extend(f"@{e} (expert)" for e in ctx.area_experts)
            people_line = f"\ncc: {', '.join(people_tags)}" if people_tags else ""

            _notify_thread(ctx, (
                f"🔬 Triage complete — Severity: *{ctx.severity.value}* | Confidence: {ctx.confidence:.0%}\n"
                f"Root cause: {ctx.root_cause[:200]}{people_line}"
            ))

            gh.add_label(issue_number, f"severity:{ctx.severity.value.lower()}")
            _notify_thread(ctx, f"🔧 Starting automated fix for #{issue_number}...")
            ctx = run_solve(ctx, gh, tracker)
            if ctx.pr_url:
                _notify_thread(ctx, f"✅ PR opened: {ctx.pr_url}")
            else:
                _notify_thread(ctx, f"⚠️ Fix aborted or rejected. Decision: {ctx.autonomy_decision.value}")
        except Exception as e:
            crashed = True
            error_msg = f"❌ Pipeline crashed: {type(e).__name__}: {e}"
            print(f"[orchestrator] #{issue_number} — {error_msg}", flush=True)
            gh.post_comment(issue_number, error_msg)
            _notify_thread(ctx, error_msg)
        finally:
            record_run(ctx, tracker, time.time() - start, crashed=crashed)
            cost_summary = tracker.summary()
            print(cost_summary, flush=True)
            _notify_thread(ctx, cost_summary)

        return ctx
    finally:
        _pipeline_lock.release()


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
