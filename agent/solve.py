import anthropic
from agent.models import BugContext, AutonomyDecision
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_diff
from agent.tools.file_tools import read_file, write_file
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


def run_solve(ctx: BugContext, gh: GitHubClient) -> BugContext:
    fix_branch = f"fix/issue-{ctx.issue_number}"
    _create_fix_branch(ctx.repo_path, fix_branch)
    ctx.fix_branch = fix_branch

    _apply_fix(ctx)

    diff = git_diff(ctx.repo_path)
    ctx.proposed_diff = diff

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


def _apply_fix(ctx: BugContext) -> None:
    messages = [{
        "role": "user",
        "content": (
            f"Fix this Vikunja bug. Read the relevant files and apply the minimal change.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Root cause: {ctx.root_cause}\n"
            f"Repo: {ctx.repo_path}\n\n"
            "After applying the fix, run the relevant tests to verify it works."
        ),
    }]

    while True:
        response = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=8192,
            thinking={"type": "adaptive"},
            tools=_SOLVE_TOOLS,
            messages=messages,
        )

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
    run_shell(
        f"git -C {ctx.repo_path} add -A && "
        f"git -C {ctx.repo_path} commit -m 'fix: resolve #{ctx.issue_number} - {ctx.issue_title[:60]}'"
    )
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
    return (
        f"## 🔧 Proposed Fix for #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**HITL required because:** {'; '.join(reasons)}\n\n"
        f"```diff\n{diff[:3000]}\n```\n\n"
        "Reply `/approve` to merge or `/reject` to abort. Timeout: 30 minutes."
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
    return (
        f"Fixes #{ctx.issue_number}\n\n"
        f"**Root cause:** {ctx.root_cause}\n\n"
        f"**Confidence:** {ctx.confidence:.0%}\n\n"
        f"**Merge decision:** {'Automatic (all autonomy criteria met)' if auto else 'Human approved via GitHub'}\n\n"
        "*Fix proposed by Claude Opus 4.8*"
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
