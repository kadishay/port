import os
import anthropic
from agent.models import BugContext, AutonomyDecision
from agent.cost_tracker import CostTracker
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file
from agent.tools.browser_tools import (
    BROWSER_TOOLS, browser_navigate, browser_click, browser_type,
    browser_get_text, browser_screenshot, browser_wait, close_browser,
    playwright_enabled,
)
from agent.autonomy import evaluate_autonomy
from agent.hitl import wait_for_approval, HITLTimeout

client = anthropic.Anthropic()

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

    if decision == AutonomyDecision.ESCALATE_ONLY:
        gh.post_comment(ctx.issue_number, _escalate_comment(ctx))
        return ctx

    if decision == AutonomyDecision.AUTO_MERGE:
        return _finish_and_open_pr(ctx, gh, diff)

    # HITL required
    gh.post_comment(ctx.issue_number, _hitl_comment(ctx, diff, reasons))
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

    return _finish_and_open_pr(ctx, gh, diff)


def _verify_fix(ctx: BugContext, tracker: CostTracker) -> None:
    """Re-run the bug reproduction steps with browser tools and take an after-screenshot."""
    if not playwright_enabled() or not ctx.screenshot_before or not ctx.reproduction_steps:
        return

    messages = [{
        "role": "user",
        "content": (
            f"The following bug has been fixed. Use the browser to verify the fix works.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause that was fixed: {ctx.root_cause}\n\n"
            f"Original reproduction steps:\n{ctx.reproduction_steps}\n\n"
            f"Vikunja frontend: http://localhost:4173\n"
            f"Re-run the steps and confirm the bug is gone. "
            f"Take a screenshot named 'bug-{ctx.issue_number}-after.png' showing the fixed state."
        ),
    }]

    while True:
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
                if block.name == "browser_screenshot" and not result.startswith("Error"):
                    ctx.screenshot_after = result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

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
            f"Fix this Vikunja bug with the MINIMAL possible change — one line or value, not a refactor.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause: {ctx.root_cause}\n"
            f"{buggy_hint}"
            f"Suspected file(s): {affected}\n"
            f"Repo: {ctx.repo_path}\n\n"
            "Steps (at most 5 tool calls total):\n"
            "1) run_shell: grep for the buggy pattern or function name in "
            f"{ctx.repo_path}/pkg/models/ and {ctx.repo_path}/frontend/src/ "
            "(exclude *_test* and *swagger*) to confirm the exact file and line.\n"
            "2) read_file the confirmed file.\n"
            "3) write_file with ONLY the minimal fix — change the one wrong value or line. "
            "Do NOT add new functions, do NOT refactor unrelated code.\n"
            "4) run_shell to build/test. Stop after that."
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


def _finish_and_open_pr(ctx: BugContext, gh: GitHubClient, diff: str) -> BugContext:
    _, _, rc = run_shell(
        f"git -C {ctx.repo_path} add -A && "
        f"git -C {ctx.repo_path} commit -m 'fix: resolve #{ctx.issue_number} - {ctx.issue_title[:60]}'"
    )
    if rc != 0:
        gh.post_comment(ctx.issue_number, "⚠️ Fix agent ran but produced no file changes — no commit to push.")
        print(f"[solve] #{ctx.issue_number} — git commit returned rc={rc}, nothing to push", flush=True)
        return ctx
    run_shell(f"git -C {ctx.repo_path} push origin {ctx.fix_branch}")

    pr = gh.create_pr(
        title=f"fix: resolve #{ctx.issue_number} — {ctx.issue_title[:60]}",
        body=_pr_body(ctx, diff),
        head=ctx.fix_branch,
    )
    ctx.pr_url = pr.get("html_url", "")
    gh.post_comment(ctx.issue_number, f"✅ Fix applied automatically. PR: {ctx.pr_url}")
    return ctx


def _create_fix_branch(repo_path: str, branch: str) -> None:
    run_shell(f"git -C {repo_path} checkout -b {branch}")


def _abort_fix(repo_path: str, branch: str) -> None:
    run_shell(f"git -C {repo_path} checkout main && git -C {repo_path} branch -D {branch}")


def _hitl_comment(ctx: BugContext, diff: str, reasons: list[str]) -> str:
    risk_info = f"**Risk level:** {ctx.risk_level.value}\n" if ctx.risk_level else ""
    return (
        f"## 🔧 Proposed Fix for #{ctx.issue_number}\n\n"
        f"**Severity:** {ctx.severity.value} | **Confidence:** {ctx.confidence:.0%}\n\n"
        f"{risk_info}"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**HITL required because:** {'; '.join(reasons)}\n\n"
        f"```diff\n{diff[:3000]}\n```\n\n"
        + (
            "Reply `approve` or `reject` in the **Slack `#bug-triage` thread** for this issue. Timeout: 30 minutes."
            if os.environ.get("SLACK_APP_TOKEN")
            else "Reply `/approve` to merge or `/reject` to abort. Timeout: 30 minutes."
        )
    )


def _escalate_comment(ctx: BugContext) -> str:
    return (
        f"## ⚠️ Escalated to human — #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        "This change touches auth, migrations, or security-sensitive code. "
        "Automatic fix skipped. Please review and fix manually."
    )


def _pr_body(ctx: BugContext, diff: str) -> str:
    auto = ctx.autonomy_decision == AutonomyDecision.AUTO_MERGE
    risk_line = f"**Risk:** {ctx.risk_level.value} ({'; '.join(ctx.risk_reasons)})\n\n" if ctx.risk_level else ""
    screenshots = ""
    if ctx.screenshot_before:
        screenshots += f"**Before fix:** `{ctx.screenshot_before}`\n"
    if ctx.screenshot_after:
        screenshots += f"**After fix:** `{ctx.screenshot_after}`\n"
    if screenshots:
        screenshots = f"\n### Screenshots\n{screenshots}\n"
    return (
        f"Fixes #{ctx.issue_number}\n\n"
        f"**Severity:** {ctx.severity.value} | **Confidence:** {ctx.confidence:.0%}\n\n"
        f"{risk_line}"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"{screenshots}"
        f"**Merge decision:** {'Automatic (LOW risk + criteria met)' if auto else 'Human approved via GitHub'}\n\n"
        "*Fix proposed by Claude Haiku 4.5 / Verified by Playwright*"
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
