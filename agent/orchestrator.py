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
    _notify_thread(ctx, (
        f"🔬 Triage complete — Severity: *{ctx.severity}* | Confidence: {ctx.confidence:.0%}\n"
        f"Root cause: {ctx.root_cause[:200]}"
    ))

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
