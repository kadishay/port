import os
import time
import threading
from agent.models import BugContext
from agent.tools.github_tools import GitHubClient


class HITLTimeout(Exception):
    pass


def wait_for_approval(
    ctx: BugContext,
    gh: GitHubClient,
    timeout_seconds: int = 1800,
    poll_interval: int = 30,
) -> bool:
    """Poll GitHub comments and Slack in parallel — first response wins."""
    decision: list[bool | None] = [None]
    done = threading.Event()

    def _poll_github():
        deadline = time.time() + timeout_seconds
        while time.time() < deadline and not done.is_set():
            try:
                for comment in gh.get_comments(ctx.issue_number):
                    body = comment.get("body", "").strip().lower()
                    if body.startswith("/approve") or body == "approve":
                        decision[0] = True
                        done.set()
                        return
                    if body.startswith("/reject") or body == "reject":
                        decision[0] = False
                        done.set()
                        return
            except Exception:
                pass
            time.sleep(poll_interval)

    def _poll_slack():
        if not os.environ.get("SLACK_APP_TOKEN") or not ctx.slack_thread_ts:
            return
        try:
            from agent.slack_client import SlackClient
            approved = SlackClient().wait_for_approval(ctx.slack_thread_ts, timeout=timeout_seconds)
            if not done.is_set():
                decision[0] = approved
                done.set()
        except HITLTimeout:
            pass
        except Exception:
            pass

    for t in [
        threading.Thread(target=_poll_github, daemon=True),
        threading.Thread(target=_poll_slack, daemon=True),
    ]:
        t.start()

    done.wait(timeout=timeout_seconds)

    if decision[0] is None:
        raise HITLTimeout(f"No response received within {timeout_seconds}s for issue #{ctx.issue_number}")

    return decision[0]
