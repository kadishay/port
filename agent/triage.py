import json
import os
import anthropic
from agent.models import BugContext, Severity
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_find_introducer_sha
from agent.tools.file_tools import read_file
from agent.tools.browser_tools import (
    BROWSER_TOOLS, browser_navigate, browser_click, browser_type,
    browser_get_text, browser_screenshot, browser_wait, close_browser,
    playwright_enabled,
)
from agent.cost_tracker import CostTracker

client = anthropic.Anthropic()

_TRIAGE_TOOLS = [
    {
        "name": "run_shell",
        "description": "Run a shell command in the Vikunja repo or against the Vikunja API.",
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


def run_triage(ctx: BugContext, gh: GitHubClient, tracker: CostTracker) -> BugContext:
    ctx.reproduction_steps = _parse_reproduction_steps(ctx, tracker)
    ctx.reproduction_log = _reproduce(ctx, ctx.reproduction_steps, tracker)

    _check_not_a_bug(ctx)
    if ctx.not_a_bug:
        _post_not_a_bug_comment(ctx, gh)
        return ctx

    ctx.root_cause, ctx.confidence, ctx.affected_files, ctx.buggy_pattern = _analyze_root_cause(ctx, tracker)
    _find_relevant_people(ctx, gh)
    ctx.severity = _classify_severity(ctx, tracker)
    _post_triage_comment(ctx, gh)
    return ctx


def _parse_reproduction_steps(ctx: BugContext, tracker: CostTracker) -> str:
    api_token = os.environ.get("VIKUNJA_API_TOKEN", "")
    api_base = os.environ.get("VIKUNJA_API_BASE", "http://localhost:3456")
    auth_note = (
        f"All curl commands MUST include the header: "
        f"-H 'Authorization: Bearer {api_token}'\n"
    ) if api_token else ""
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the exact shell/curl commands needed to reproduce this bug.\n\n"
                f"Issue: {ctx.issue_title}\n\n{ctx.issue_body}\n\n"
                f"Vikunja API base: {api_base}\n"
                f"{auth_note}"
                "List only the commands, one per line."
            ),
        }],
    )
    tracker.record(response.model, response.usage)
    return next(b.text for b in response.content if b.type == "text")


def _reproduce(ctx: BugContext, steps: str, tracker: CostTracker) -> str:
    api_token = os.environ.get("VIKUNJA_API_TOKEN", "")
    vikunja_username = os.environ.get("VIKUNJA_USERNAME", "")
    vikunja_password = os.environ.get("VIKUNJA_PASSWORD", "")
    auth_header = f"-H 'Authorization: Bearer {api_token}'" if api_token else ""

    is_ui_bug = any(kw in ctx.issue_title.lower() for kw in ("frontend", "ui", "kanban", "vue", "display", "button", "click", "drag"))
    use_browser = playwright_enabled() and is_ui_bug

    login_instruction = ""
    if use_browser and vikunja_username and vikunja_password:
        login_instruction = (
            f"Before reproducing: navigate to http://localhost:4173, log in with "
            f"username '{vikunja_username}' and password '{vikunja_password}'. "
        )

    messages = [{
        "role": "user",
        "content": (
            f"Reproduce this bug by running these steps against the Vikunja instance.\n\n"
            f"Steps:\n{steps}\n\n"
            f"Vikunja repo: {ctx.repo_path}\n"
            f"Vikunja API: {os.environ.get('VIKUNJA_API_BASE', 'http://localhost:3456')}\n"
            f"API auth header for all curl commands: {auth_header}\n\n"
            "RULES:\n"
            "- For backend/API bugs: use ONLY run_shell with curl (include the auth header above). "
            "Do NOT use browser tools for backend bugs.\n"
            "- For UI/frontend bugs: use the browser_* tools to drive the frontend. "
            f"{login_instruction}"
            "After demonstrating the bug, take a browser_screenshot with a descriptive filename.\n"
            "When done, summarize what you observed."
        ),
    }]
    log_parts: list[str] = []
    screenshot_paths: list[str] = []

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=_TRIAGE_TOOLS + (BROWSER_TOOLS if playwright_enabled() else []),
            messages=messages,
        )

        tracker.record(response.model, response.usage)
        if response.stop_reason == "end_turn":
            log_parts.extend(b.text for b in response.content if b.type == "text")
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, ctx.repo_path)
                log_parts.append(f"[{block.name}] {block.input}\n{result}")
                if block.name == "browser_screenshot" and not result.startswith("Error"):
                    screenshot_paths.append(result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    close_browser()
    if screenshot_paths:
        ctx.screenshot_before = screenshot_paths[-1]

    return "\n".join(log_parts)


def _check_not_a_bug(ctx: BugContext) -> None:
    # TODO: replace with real documentation lookup (vikunja.io/docs or scraped local copy)
    ctx.not_a_bug = False


def _post_not_a_bug_comment(ctx: BugContext, gh: GitHubClient) -> None:
    gh.post_comment(
        ctx.issue_number,
        f"## 🤖 Automated Triage — Not a Bug\n\n"
        f"After reviewing the Vikunja documentation and source code, this appears to be "
        f"**expected behaviour** rather than a bug.\n\n"
        f"**Reason:** {ctx.not_a_bug_reason}\n\n"
        f"If you believe this is incorrect, please add more context and reopen the issue.\n\n"
        f"*Triage powered by Claude Haiku 4.5*"
    )
    gh.add_label(ctx.issue_number, "not-a-bug")


def _analyze_root_cause(ctx: BugContext, tracker: CostTracker) -> tuple[str, float, list[str], str]:
    response = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2048,
        thinking={"type": "adaptive"},
        messages=[{
            "role": "user",
            "content": (
                f"Analyze this Vikunja bug. Identify the root cause and your confidence (0.0–1.0).\n\n"
                f"Issue: {ctx.issue_title}\n\n"
                f"Reproduction log:\n{ctx.reproduction_log}\n\n"
                "Respond with JSON only:\n"
                '{"root_cause": "...", "confidence": 0.XX, '
                '"files": ["relative/path/to/file.go"], '
                '"buggy_pattern": "exact string that is wrong in the source"}'
            ),
        }],
    )
    tracker.record(response.model, response.usage)
    text = next(b.text for b in response.content if b.type == "text")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(cleaned)
    return (
        data["root_cause"],
        float(data["confidence"]),
        data.get("files", []),
        data.get("buggy_pattern", ""),
    )


def _find_relevant_people(ctx: BugContext, gh: GitHubClient) -> None:
    if not ctx.affected_files or not ctx.buggy_pattern:
        return
    filepath = ctx.affected_files[0]
    rel_path = filepath.removeprefix(ctx.repo_path).lstrip("/")

    sha = git_find_introducer_sha(ctx.repo_path, rel_path, ctx.buggy_pattern)
    if sha:
        ctx.blame_author = gh.get_commit_author_login(sha) or ""

    all_experts = gh.get_file_top_authors(rel_path, n=3)
    ctx.area_experts = [e for e in all_experts if e != ctx.blame_author][:2]


def _classify_severity(ctx: BugContext, tracker: CostTracker) -> Severity:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=256,
        messages=[{
            "role": "user",
            "content": (
                f"Classify the severity of this bug. Root cause: {ctx.root_cause}\n\n"
                "Rubric:\n"
                "- CRITICAL: data loss, auth bypass, crash on startup\n"
                "- HIGH: wrong data shown, core feature broken\n"
                "- MEDIUM: degraded UX, edge case error\n"
                "- LOW: visual glitch, non-blocking\n\n"
                'Respond with JSON only: {"severity": "HIGH"}'
            ),
        }],
    )
    tracker.record(response.model, response.usage)
    text = next(b.text for b in response.content if b.type == "text")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(cleaned)
    return Severity(data["severity"])


def _post_triage_comment(ctx: BugContext, gh: GitHubClient) -> None:
    severity_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}

    mentions: list[str] = []
    if ctx.blame_author:
        mentions.append(f"@{ctx.blame_author} (introduced the bug)")
    mentions.extend(f"@{e} (area expert)" for e in ctx.area_experts)
    people_section = (
        "### People to Notify\n" + "\n".join(f"- {m}" for m in mentions) + "\n\n"
        if mentions else ""
    )

    screenshot_section = (
        f"### Screenshot (Before Fix)\n`{ctx.screenshot_before}`\n\n"
        if ctx.screenshot_before else ""
    )

    body = (
        f"## 🤖 Automated Triage Report\n\n"
        f"**Severity:** {severity_emoji.get(ctx.severity.value, '⚪')} {ctx.severity.value}\n"
        f"**Confidence:** {ctx.confidence:.0%}\n\n"
        f"### Root Cause\n{ctx.root_cause}\n\n"
        f"{people_section}"
        f"{screenshot_section}"
        f"### Reproduction Log\n```\n{ctx.reproduction_log[:2000]}\n```\n\n"
        f"*Triage powered by Claude Opus 4.8*"
    )
    gh.post_comment(ctx.issue_number, body)


def _execute_tool(name: str, inputs: dict, repo_path: str) -> str:
    if name == "run_shell":
        stdout, stderr, rc = run_shell(inputs["cmd"], cwd=repo_path, timeout=inputs.get("timeout", 60))
        return f"[exit {rc}]\nstdout: {stdout}\nstderr: {stderr}"
    if name == "read_file":
        try:
            return read_file(inputs["path"])
        except (FileNotFoundError, IsADirectoryError, OSError) as e:
            return f"Error reading file: {e}"
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
    return f"Unknown tool: {name}"
