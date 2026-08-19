import os
import anthropic
from agent.models import BugContext, AutonomyDecision
from agent.cost_tracker import CostTracker
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file
from agent.tools.browser_tools import (
    BROWSER_TOOLS, browser_navigate, browser_click, browser_type,
    browser_get_text, browser_screenshot, browser_wait, browser_evaluate,
    browser_press, browser_go_back, close_browser, playwright_enabled,
)
from agent.autonomy import evaluate_autonomy
from agent.hitl import wait_for_approval, HITLTimeout
from agent.triage import _is_frontend_bug

client = anthropic.Anthropic()

_VERIFY_TOOLS = [
    {
        "name": "run_shell",
        "description": "Run a shell command (curl against the Vikunja API, git, etc). Read-only — do not modify files here.",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "timeout": {"type": "integer", "default": 60},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a source file from the Vikunja repo.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]

_REPORT_VERIFICATION_TOOL = {
    "name": "report_verification",
    "description": "Report whether re-running the reproduction steps shows the bug is fixed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fixed": {"type": "boolean", "description": "true if the bug no longer reproduces"},
            "evidence": {"type": "string", "description": "What you observed — e.g. curl status/body before vs after"},
        },
        "required": ["fixed", "evidence"],
    },
}

_SOLVE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a source file from the Vikunja repo.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write (overwrite) a file in the Vikunja repo with the fixed content.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command (build, test, git operations).",
        "input_schema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "timeout": {"type": "integer", "default": 120},
            },
            "required": ["cmd"],
        },
    },
]


def run_solve(ctx: BugContext, gh: GitHubClient, tracker: CostTracker) -> BugContext:
    fix_branch = f"fix/issue-{ctx.issue_number}"
    _create_fix_branch(ctx.repo_path, fix_branch)
    ctx.fix_branch = fix_branch

    print(f"[solve] #{ctx.issue_number} — applying fix", flush=True)
    _apply_fix(ctx, tracker)

    print(f"[solve] #{ctx.issue_number} — verifying fix", flush=True)
    _verify_fix(ctx, tracker)

    diff = git_diff(ctx.repo_path)
    ctx.proposed_diff = diff

    print(f"[solve] #{ctx.issue_number} — evaluating autonomy", flush=True)
    decision, reasons = evaluate_autonomy(ctx, diff)
    ctx.autonomy_decision = decision
    ctx.autonomy_reasons = reasons

    # Commit fix now — before any HITL wait — so external git ops can't wipe uncommitted changes
    safe_title = ctx.issue_title[:50].replace("'", "")
    _, commit_out, rc = run_shell(
        f"git -C {ctx.repo_path} add -A && "
        f"git -C {ctx.repo_path} commit -m 'fix: proposed fix for #{ctx.issue_number} — {safe_title}'"
    )
    if rc != 0:
        msg = "⚠️ Fix agent ran but produced no file changes — no commit to push."
        gh.post_comment(ctx.issue_number, msg)
        print(f"[solve] #{ctx.issue_number} — {msg}", flush=True)
        _abort_fix(ctx.repo_path, fix_branch)
        return ctx

    if decision == AutonomyDecision.AUTO_PR:
        return _push_and_open_pr(ctx, gh)

    # HITL required — notify both GitHub and Slack
    gh.post_comment(ctx.issue_number, _hitl_comment(ctx, diff, reasons))
    _notify_hitl_slack(ctx, diff, reasons)

    try:
        approved = wait_for_approval(ctx, gh)
    except HITLTimeout:
        gh.post_comment(ctx.issue_number, "⏰ No response received within 30 minutes. Auto-fix aborted.")
        _abort_fix(ctx.repo_path, fix_branch)
        return ctx

    if not approved:
        gh.post_comment(ctx.issue_number, "❌ Fix rejected by human reviewer. Branch deleted.")
        _abort_fix(ctx.repo_path, fix_branch)
        return ctx

    return _push_and_open_pr(ctx, gh)


def _verify_fix(ctx: BugContext, tracker: CostTracker) -> None:
    if _is_frontend_bug(ctx.issue_title):
        _verify_fix_frontend(ctx, tracker)
    else:
        _verify_fix_backend(ctx, tracker)


def _verify_fix_backend(ctx: BugContext, tracker: CostTracker) -> None:
    """Re-run the reproduction curl commands against the live API and confirm the bug is gone."""
    if not ctx.reproduction_steps:
        return

    api_token = os.environ.get("VIKUNJA_API_TOKEN", "")
    api_base = os.environ.get("VIKUNJA_API_BASE", "http://localhost:3456")
    auth_header = f"-H 'Authorization: Bearer {api_token}'" if api_token else ""

    messages = [{
        "role": "user",
        "content": (
            f"This bug has just been fixed in the code. Re-run the original reproduction steps "
            f"against the live API to confirm the bug is actually gone.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause that was fixed: {ctx.root_cause}\n\n"
            f"Original reproduction steps:\n{ctx.reproduction_steps}\n\n"
            f"Vikunja API base: {api_base}\n"
            f"API auth header: {auth_header}\n\n"
            "Use run_shell (curl) to re-run the same requests. Compare the result against the "
            "'Actual result' described in the issue — if that behaviour no longer happens, the fix works.\n"
            "At most 5 tool calls, then call report_verification with your verdict."
        ),
    }]

    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            tools=[*_VERIFY_TOOLS, _REPORT_VERIFICATION_TOOL],
            messages=messages,
        )
        tracker.record(response.model, response.usage)

        for block in response.content:
            if block.type == "tool_use" and block.name == "report_verification":
                d = block.input
                ctx.fix_verified = bool(d.get("fixed", False))
                ctx.verification_log = d.get("evidence", "")
                print(
                    f"[solve] #{ctx.issue_number} — verify via curl: "
                    f"{'PASS' if ctx.fix_verified else 'FAIL'} — {ctx.verification_log[:200]}",
                    flush=True,
                )
                return

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, ctx.repo_path)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Model never called report_verification (e.g. kept calling tools) — force a verdict.
    _force_verification_verdict(ctx, tracker)


def _force_verification_verdict(ctx: BugContext, tracker: CostTracker) -> None:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=512,
        tools=[_REPORT_VERIFICATION_TOOL],
        tool_choice={"type": "tool", "name": "report_verification"},
        messages=[{
            "role": "user",
            "content": (
                f"Issue: {ctx.issue_title}\nRoot cause fixed: {ctx.root_cause}\n\n"
                f"You ran out of tool calls while re-verifying this fix. Based on what you saw so far, "
                "call report_verification with your best-effort verdict."
            ),
        }],
    )
    tracker.record(response.model, response.usage)
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_verification":
            d = block.input
            ctx.fix_verified = bool(d.get("fixed", False))
            ctx.verification_log = d.get("evidence", "inconclusive — ran out of tool calls")
            print(
                f"[solve] #{ctx.issue_number} — verify via curl (forced): "
                f"{'PASS' if ctx.fix_verified else 'FAIL'} — {ctx.verification_log[:200]}",
                flush=True,
            )
            return
    ctx.fix_verified = False
    ctx.verification_log = "verification inconclusive — model returned no verdict"


def _verify_fix_frontend(ctx: BugContext, tracker: CostTracker) -> None:
    """Re-run the bug reproduction steps with browser tools and take an after-screenshot."""
    if not playwright_enabled() or not ctx.screenshot_before or not ctx.reproduction_steps:
        return

    vikunja_username = os.environ.get("VIKUNJA_USERNAME", "")
    vikunja_password = os.environ.get("VIKUNJA_PASSWORD", "")
    is_kanban = "kanban" in (ctx.issue_title or "").lower()
    kanban_verify = (
        "\nKANBAN VERIFY STEPS (follow exactly):\n"
        "1. browser_navigate 'http://localhost:4173'\n"
        "2. browser_evaluate 'localStorage.setItem(\"API_URL\", \"http://localhost:3456\")'\n"
        "3. browser_navigate 'http://localhost:4173'\n"
        "4. browser_wait 1000ms\n"
        f"5. browser_type '#username' '{vikunja_username}'\n"
        f"6. browser_type '#password' '{vikunja_password}'\n"
        "7. browser_press '#password' 'Enter'\n"
        "8. browser_wait 2000ms\n"
        "9. browser_navigate 'http://localhost:4173/projects/3/20'\n"
        "10. browser_wait 3000ms for Kanban to load\n"
        "11. browser_click '.kanban-card__title-link'  (click first task in To-Do)\n"
        "12. browser_wait 2000ms\n"
        "13. browser_click '.button--mark-done'\n"
        "14. browser_wait 2000ms\n"
        "15. browser_go_back  (IMPORTANT: use go_back, not navigate, to keep Kanban state in memory)\n"
        "16. browser_wait 3000ms\n"
        "After step 16, STOP immediately — do NOT call browser_screenshot. "
        "A screenshot will be taken automatically.\n"
        if is_kanban else ""
    )

    messages = [{
        "role": "user",
        "content": (
            f"The following bug has been fixed. Use the browser to verify the fix works.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause that was fixed: {ctx.root_cause}\n\n"
            f"Original reproduction steps:\n{ctx.reproduction_steps}\n\n"
            f"Vikunja frontend: http://localhost:4173\n"
            f"{kanban_verify}"
            "Re-run the steps and confirm the bug is gone. "
            f"Take a screenshot named 'bug-{ctx.issue_number}-after.png' showing the fixed state."
        ),
    }]

    for _ in range(12):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=BROWSER_TOOLS,
            messages=messages,
        )

        tracker.record(response.model, response.usage)
        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_browser_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    # Take the after-screenshot programmatically with a canonical name
    canonical = f"bug-{ctx.issue_number}-after.png"
    path = browser_screenshot(canonical)
    if not path.startswith("Error"):
        ctx.screenshot_after = path
        print(f"[solve] after-screenshot: {path}", flush=True)

    close_browser()


def _execute_browser_tool(name: str, inputs: dict) -> str:
    if name == "browser_navigate":
        return browser_navigate(inputs["url"])
    if name == "browser_click":
        return browser_click(inputs["selector"])
    if name == "browser_type":
        return browser_type(inputs["selector"], inputs["text"])
    if name == "browser_get_text":
        return browser_get_text(inputs["selector"])
    if name == "browser_screenshot":
        return browser_screenshot(inputs["filename"])
    if name == "browser_wait":
        return browser_wait(inputs.get("milliseconds", 1000))
    if name == "browser_evaluate":
        return browser_evaluate(inputs["script"])
    if name == "browser_press":
        return browser_press(inputs["selector"], inputs["key"])
    if name == "browser_go_back":
        return browser_go_back()
    return f"Unknown browser tool: {name}"


def _apply_fix(ctx: BugContext, tracker: CostTracker) -> None:
    affected = ", ".join(ctx.affected_files) if ctx.affected_files else "unknown"
    buggy_hint = (
        f"Buggy pattern to locate: `{ctx.buggy_pattern}`\n"
        if ctx.buggy_pattern else ""
    )
    messages = [{
        "role": "user",
        "content": (
            f"Fix this Vikunja bug by restoring the ONE value that was changed to introduce the bug.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause: {ctx.root_cause}\n"
            f"{buggy_hint}"
            f"Suspected file(s): {affected}\n"
            f"Repo: {ctx.repo_path}\n\n"
            "Steps (at most 6 tool calls total):\n"
            "1) run_shell: find the exact file by grepping for the buggy pattern "
            f"in {ctx.repo_path}/pkg/models/ and {ctx.repo_path}/frontend/src/ "
            "(exclude *_test* and *swagger*).\n"
            "2) run_shell: check git history to find what the original value was BEFORE the bug:\n"
            f"   git -C {ctx.repo_path} log --oneline -5 -- <file>\n"
            f"   git -C {ctx.repo_path} show <prev-sha>:<relative-file-path> | grep -A2 -B2 '<buggy_term>'\n"
            "3) read_file the confirmed file.\n"
            "4) write_file — change EXACTLY ONE value: replace the buggy value with the original "
            "value from git history. Touch nothing else. No new functions, no reformatting, "
            "no 'while you're here' fixes to nearby lines.\n"
            "5) run_shell to build/test. Stop after that."
        ),
    }]

    for iteration in range(8):
        print(f"[solve] #{ctx.issue_number} — apply_fix iteration {iteration + 1}", flush=True)
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=8192,
            tools=_SOLVE_TOOLS,
            messages=messages,
        )

        tracker.record(response.model, response.usage)
        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, ctx.repo_path)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})


def _push_and_open_pr(ctx: BugContext, gh: GitHubClient) -> BugContext:
    run_shell(f"git -C {ctx.repo_path} push origin {ctx.fix_branch}")

    pr = gh.create_pr(
        title=f"fix: resolve #{ctx.issue_number} — {ctx.issue_title[:60]}",
        body=_pr_body(ctx, ctx.proposed_diff),
        head=ctx.fix_branch,
    )
    ctx.pr_url = pr.get("html_url", "")
    gh.post_comment(ctx.issue_number, f"✅ Fix applied automatically. PR: {ctx.pr_url}")
    return ctx


def _create_fix_branch(repo_path: str, branch: str) -> None:
    # Always branch from main so we don't accidentally build on a previous fix branch
    run_shell(f"git -C {repo_path} checkout main")
    # Delete stale branch from a previous failed run, if any
    run_shell(f"git -C {repo_path} branch -D {branch}")
    run_shell(f"git -C {repo_path} checkout -b {branch}")


def _abort_fix(repo_path: str, branch: str) -> None:
    run_shell(f"git -C {repo_path} checkout main && git -C {repo_path} branch -D {branch}")


def _notify_hitl_slack(ctx: BugContext, diff: str, reasons: list[str]) -> None:
    if not os.environ.get("SLACK_BOT_TOKEN") or not ctx.slack_thread_ts:
        return
    from agent.slack_client import SlackClient
    short_diff = diff[:1500] if diff else "(no diff)"
    msg = (
        f"🔧 *Fix ready for review — Issue #{ctx.issue_number}*\n"
        f"*Root cause:* {ctx.root_cause[:200]}\n"
        f"*Why HITL:* {'; '.join(reasons[:2])}\n\n"
        f"```{short_diff}```\n\n"
        f"Reply `approve` to merge or `reject` to abort (30 min timeout)."
    )
    SlackClient().post_to_thread(ctx.slack_thread_ts, msg)


def _hitl_comment(ctx: BugContext, diff: str, reasons: list[str]) -> str:
    risk_info = f"**Risk level:** {ctx.risk_level.value}\n" if ctx.risk_level else ""
    approval_note = (
        "Reply `approve` or `reject` here **or** in the Slack `#bug-triage` thread — whichever fires first wins. Timeout: 30 minutes."
        if os.environ.get("SLACK_APP_TOKEN")
        else "Reply `/approve` to merge or `/reject` to abort. Timeout: 30 minutes."
    )
    return (
        f"## 🔧 Proposed Fix for #{ctx.issue_number}\n\n"
        f"**Severity:** {ctx.severity.value} | **Confidence:** {ctx.confidence:.0%}\n\n"
        f"{risk_info}"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**HITL required because:** {'; '.join(reasons)}\n\n"
        f"```diff\n{diff[:3000]}\n```\n\n"
        f"{approval_note}"
    )


def _pr_body(ctx: BugContext, diff: str) -> str:
    auto = ctx.autonomy_decision == AutonomyDecision.AUTO_PR
    risk_line = f"**Risk:** {ctx.risk_level.value} ({'; '.join(ctx.risk_reasons)})\n\n" if ctx.risk_level else ""
    screenshots = ""
    if ctx.screenshot_before:
        screenshots += f"**Before fix:** `{ctx.screenshot_before}`\n"
    if ctx.screenshot_after:
        screenshots += f"**After fix:** `{ctx.screenshot_after}`\n"
    if screenshots:
        screenshots = f"\n### Screenshots\n{screenshots}\n"

    verification = ""
    verified_by = "Not independently verified"
    if ctx.screenshot_after:
        verified_by = "Verified by Playwright"
    elif ctx.verification_log:
        status = "✅ Verified" if ctx.fix_verified else "⚠️ Verification inconclusive"
        verified_by = "Verified via curl"
        verification = f"\n### Verification (curl)\n**{status}**\n```\n{ctx.verification_log[:1500]}\n```\n"

    return (
        f"Fixes #{ctx.issue_number}\n\n"
        f"**Severity:** {ctx.severity.value} | **Confidence:** {ctx.confidence:.0%}\n\n"
        f"{risk_line}"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"{screenshots}"
        f"{verification}"
        f"**Merge decision:** {'Automatic (LOW risk + criteria met)' if auto else 'Human approved via GitHub'}\n\n"
        f"*Fix proposed by Claude Haiku 4.5 / {verified_by}*"
    )


def _execute_tool(name: str, inputs: dict, repo_path: str) -> str:
    if name == "read_file":
        try:
            return read_file(inputs["path"])
        except FileNotFoundError:
            return f"File not found: {inputs['path']}"
    if name == "write_file":
        write_file(inputs["path"], inputs["content"])
        return f"Written: {inputs['path']}"
    if name == "run_shell":
        stdout, stderr, rc = run_shell(inputs["cmd"], cwd=repo_path, timeout=inputs.get("timeout", 120))
        return f"[exit {rc}]\n{stdout}\n{stderr}"
    return f"Unknown tool: {name}"
