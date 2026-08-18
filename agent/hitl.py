import os
import time
from agent.models import BugContext
from agent.tools.github_tools import GitHubClient


class HITLTimeout(Exception):
    pass


def wait_for_approval(
    ctx: BugContext,
    gh: GitHubClient,
    timeout_seconds: int = 1800,
    poll_interval: int = 60,
) -> bool:
    if os.environ.get("SLACK_APP_TOKEN") and ctx.slack_thread_ts:
        from agent.slack_client import SlackClient
        return SlackClient().wait_for_approval(ctx.slack_thread_ts, timeout=timeout_seconds)

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        comments = gh.get_comments(ctx.issue_number)
        for comment in comments:
            body = comment.get("body", "").strip().lower()
            if body.startswith("/approve"):
                return True
            if body.startswith("/reject"):
                return False
        time.sleep(poll_interval)

    raise HITLTimeout(f"No response received within {timeout_seconds}s for issue #{ctx.issue_number}")
