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


def _extract_json(text: str) -> dict:
    import re
    # Try to find a JSON object in the text
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    # Fallback: strip markdown fences and parse
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)

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
    print(f"[triage] #{ctx.issue_number} — parsing reproduction steps", flush=True)
    ctx.reproduction_steps = _parse_reproduction_steps(ctx, tracker)

    print(f"[triage] #{ctx.issue_number} — reproducing bug", flush=True)
    ctx.reproduction_log = _reproduce(ctx, ctx.reproduction_steps, tracker)

    _check_not_a_bug(ctx)
    if ctx.not_a_bug:
        _post_not_a_bug_comment(ctx, gh)
        return ctx

    import time as _time
    print(f"[triage] #{ctx.issue_number} — root cause analysis", flush=True)
    _t0 = _time.time()
    ctx.root_cause, ctx.confidence, ctx.affected_files, ctx.buggy_pattern = _analyze_root_cause(ctx, tracker)
    print(f"[triage] #{ctx.issue_number} — root cause done in {_time.time() - _t0:.1f}s", flush=True)

    print(f"[triage] #{ctx.issue_number} — finding relevant people", flush=True)
    _find_relevant_people(ctx, gh)

    print(f"[triage] #{ctx.issue_number} — classifying severity", flush=True)
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
            f"Investigate this bug in at most 4 tool calls total, then stop.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Steps:\n{steps}\n\n"
            f"Vikunja repo: {ctx.repo_path}\n"
            f"Vikunja API: {os.environ.get('VIKUNJA_API_BASE', 'http://localhost:3456')}\n"
            f"API auth header: {auth_header}\n\n"
            "RULES:\n"
            "- Do NOT try to create new tasks or users — use existing data.\n"
            "- macOS date syntax: use $(date -v+1d +%s) for 'tomorrow', NOT date -d.\n"
            "- For backend bugs: use run_shell (curl with auth header) AND read_file on the "
            "most relevant source file in the repo. Do NOT use browser tools.\n"
            "- For UI/frontend bugs: use the browser_* tools. "
            f"{login_instruction}"
            "Take a browser_screenshot after demonstrating the bug.\n"
            "- Stop after 4 tool calls. Summarize what you observed."
        ),
    }]
    log_parts: list[str] = []
    screenshot_paths: list[str] = []
    max_iterations = 5

    for iteration in range(max_iterations):
        print(f"[triage] #{ctx.issue_number} — reproduce iteration {iteration + 1}", flush=True)
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
    else:
        print(f"[triage] #{ctx.issue_number} — reproduce hit max iterations ({max_iterations}), stopping", flush=True)
        log_parts.append(f"[truncated after {max_iterations} tool iterations]")

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


_ROOT_CAUSE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a source file from the Vikunja repo to inspect the code.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to the file"}},
            "required": ["path"],
        },
    },
    {
        "name": "run_shell",
        "description": "Run a shell command to search for relevant source files (grep, find). Use to locate the buggy file before reading it.",
        "input_schema": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
]


def _analyze_root_cause(ctx: BugContext, tracker: CostTracker) -> tuple[str, float, list[str], str]:
    # Haiku is fast (~5-10s) and sufficient for single-file logic bugs.
    # Opus was 30-60s+ with thinking; switched to Haiku + capped at 5 iterations.
    messages = [{
        "role": "user",
        "content": (
            f"Analyze this Vikunja bug. Find and read the ONE source file that contains the bug. "
            f"Use this exact search order (max 3 run_shell calls, then 1 read_file):\n"
            f"1. Extract specific numbers/strings from the issue title and grep for them:\n"
            f"   grep -r '<key term from issue>' {ctx.repo_path}/pkg/models/ {ctx.repo_path}/frontend/src/ "
            f"--include='*.go' --include='*.ts' --exclude='*_test*' --exclude='*swagger*' -l | head -10\n"
            f"2. If step 1 finds nothing, grep by filename pattern:\n"
            f"   find {ctx.repo_path}/pkg/models {ctx.repo_path}/frontend/src -name '*.go' -o -name '*.ts' | "
            f"grep -v '_test' | grep -v swagger | head -20\n"
            f"3. read_file the most relevant file from the results.\n"
            f"NEVER read a _test.go or swagger file.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Repo: {ctx.repo_path}\n\n"
            f"Reproduction log:\n{ctx.reproduction_log[:3000]}\n\n"
            "Respond with JSON only:\n"
            '{"root_cause": "...", "confidence": 0.XX, '
            '"files": ["relative/path/to/file.go"], '
            '"buggy_pattern": "exact string that is wrong in the source"}'
        ),
    }]

    for _ in range(5):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=_ROOT_CAUSE_TOOLS,
            messages=messages,
        )
        tracker.record(response.model, response.usage)

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                if block.name == "read_file":
                    try:
                        result = read_file(block.input["path"])
                    except (FileNotFoundError, IsADirectoryError, OSError) as e:
                        result = f"Error: {e}"
                elif block.name == "run_shell":
                    stdout, stderr, _ = run_shell(block.input["cmd"], cwd=ctx.repo_path)
                    result = (stdout + stderr)[:4000]
                else:
                    result = f"Unknown tool: {block.name}"
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result[:8000],
                })

        if not tool_results:
            break

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ValueError(f"Opus returned no text block. Content types: {[b.type for b in response.content]}")
    data = _extract_json(text)
    return (
        data["root_cause"],
        float(data["confidence"]),
        data.get("files", []),
        data.get("buggy_pattern", ""),
    )


def _find_relevant_people(ctx: BugContext, gh: GitHubClient) -> None:
    notify_user = os.environ.get("NOTIFY_USER", "")
    if notify_user:
        ctx.blame_author = notify_user
        return

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
    data = _extract_json(text)
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
        f"*Triage powered by Claude Haiku 4.5*"
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
