import json
import os
import anthropic
from agent.models import BugContext, Severity
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell, git_find_introducer_sha
from agent.tools.file_tools import read_file
from agent.tools.browser_tools import (
    BROWSER_TOOLS, browser_navigate, browser_click, browser_type,
    browser_get_text, browser_screenshot, browser_wait, browser_evaluate,
    browser_press, browser_go_back, close_browser, playwright_enabled,
)
from agent.cost_tracker import CostTracker

client = anthropic.Anthropic()


def _extract_json(text: str) -> dict:
    import re
    # Walk from each '{"' (JSON object start), balance braces to find the complete object.
    # This avoids grabbing Go/TS code blocks that also contain '{'.
    for m in re.finditer(r'\{"', text):
        start = m.start()
        depth = 0
        for i, ch in enumerate(text[start:]):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:start + i + 1])
                    except json.JSONDecodeError:
                        break  # try next '{"'
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


_FRONTEND_KEYWORDS = ("kanban", "frontend", "ui", "vue", "view", "button", "click", "drag", "display", "bucket", "column")
_BACKEND_KEYWORDS = ("reminder", "email", "cron", "api", "webhook", "query", "migration", "go", "backend")

def _is_frontend_bug(title: str) -> bool:
    t = title.lower()
    fe = sum(1 for kw in _FRONTEND_KEYWORDS if kw in t)
    be = sum(1 for kw in _BACKEND_KEYWORDS if kw in t)
    return fe >= be  # default to frontend on tie


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

    is_ui_bug = _is_frontend_bug(ctx.issue_title)
    use_browser = playwright_enabled() and is_ui_bug

    if is_ui_bug:
        path = "frontend + Playwright" if use_browser else "frontend (no Playwright — PLAYWRIGHT_ENABLED=false)"
    else:
        path = "backend"
    print(f"[triage] #{ctx.issue_number} — reproduce path: {path}", flush=True)

    login_instruction = ""
    if use_browser and vikunja_username and vikunja_password:
        login_instruction = (
            f"Before reproducing: browser_navigate 'http://localhost:4173', "
            f"browser_evaluate 'localStorage.setItem(\"API_URL\",\"http://localhost:3456\")', "
            f"browser_navigate 'http://localhost:4173', browser_wait 1000ms, "
            f"browser_type '#username' '{vikunja_username}', browser_type '#password' '{vikunja_password}', "
            f"browser_press '#password' 'Enter', browser_wait 2000ms. "
        )

    max_tool_calls = 20 if use_browser else 4
    kanban_hint = (
        f"\nKANBAN STEPS — call these browser tools IN THIS EXACT ORDER, one per tool call:\n"
        f"1) browser_navigate 'http://localhost:4173'\n"
        f"2) browser_evaluate 'localStorage.setItem(\"API_URL\",\"http://localhost:3456\")'\n"
        f"3) browser_navigate 'http://localhost:4173'\n"
        f"4) browser_wait 1000\n"
        f"5) browser_type '#username' '{vikunja_username}'\n"
        f"6) browser_type '#password' '{vikunja_password}'\n"
        f"7) browser_press '#password' 'Enter'\n"
        f"8) browser_wait 2000\n"
        f"9) browser_navigate 'http://localhost:4173/projects/3/20'\n"
        f"10) browser_wait 3000\n"
        f"11) browser_screenshot 'bug-{ctx.issue_number}-before.png'\n"
        f"12) browser_click '.kanban-card__title-link'\n"
        f"13) browser_wait 2000\n"
        f"14) browser_click '.button--mark-done'\n"
        f"15) browser_wait 2000\n"
        f"16) browser_go_back\n"
        f"17) browser_wait 2000\n"
        f"18) browser_screenshot 'bug-{ctx.issue_number}-before.png'\n"
        f"After step 18, STOP all tool calls. The bug is visible in the last screenshot: "
        f"task has 'Done' badge but remained in To-Do column. Summarize what you saw.\n"
        if (use_browser and "kanban" in ctx.issue_title.lower()) else ""
    )

    messages = [{
        "role": "user",
        "content": (
            f"Investigate this bug in at most {max_tool_calls} tool calls total, then stop.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Steps:\n{steps}\n\n"
            f"Vikunja repo: {ctx.repo_path}\n"
            f"Vikunja API: {os.environ.get('VIKUNJA_API_BASE', 'http://localhost:3456')}\n"
            f"API auth header: {auth_header}\n\n"
            f"BUG TYPE: {'FRONTEND (TypeScript/Vue)' if is_ui_bug else 'BACKEND (Go)'}\n\n"
            "RULES:\n"
            "- Do NOT try to create new tasks or users — use existing data.\n"
            "- macOS date syntax: use $(date -v+1d +%s) for 'tomorrow', NOT date -d.\n"
            + (
                f"- This is a FRONTEND bug. Search {ctx.repo_path}/frontend/src/ for .ts/.vue files. "
                f"Do NOT read .go files. "
                + (f"{login_instruction}Use browser_* tools to reproduce visually. "
                   f"{kanban_hint}"
                   f"Take a final browser_screenshot named 'bug-{ctx.issue_number}-before.png' showing the bug state.\n"
                   if use_browser else
                   f"Run: find {ctx.repo_path}/frontend/src/stores -name '*.ts' | head -10, "
                   "then read_file the most relevant TypeScript store file.\n")
                if is_ui_bug else
                "- This is a BACKEND bug. Use run_shell (curl with auth header) AND read_file on the "
                "most relevant source file in pkg/models/. Do NOT use browser tools.\n"
            ) +
            f"- Stop after {max_tool_calls} tool calls. Summarize what you observed."
        ),
    }]
    log_parts: list[str] = []
    screenshot_paths: list[str] = []
    max_iterations = 20 if use_browser else 5

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
    is_fe = _is_frontend_bug(ctx.issue_title)
    search_dir = f"{ctx.repo_path}/frontend/src" if is_fe else f"{ctx.repo_path}/pkg/models"
    file_ext = "*.ts or *.vue" if is_fe else "*.go"
    bug_type = "FRONTEND (TypeScript/Vue)" if is_fe else "BACKEND (Go)"

    messages = [{
        "role": "user",
        "content": (
            f"Find the root cause of this Vikunja bug. Use up to 6 tool calls. Work systematically:\n\n"
            f"BUG TYPE: {bug_type}\n"
            f"SEARCH DIRECTORY: {search_dir}\n"
            f"FILE TYPES: {file_ext} ONLY — do NOT read files of other types.\n\n"
            f"SEARCH STRATEGY (follow in order):\n"
            f"1. FILENAME FIRST — find files whose NAME contains a keyword from the issue title:\n"
            f"   find {search_dir} -name '*<keyword>*' | grep -v test | grep -v node_modules\n"
            f"2. READ THE FILE — read_file the best candidate. Look for the specific wrong value/condition.\n"
            f"3. IF UNCERTAIN — if you can't spot the bug in the first file, search for one more.\n"
            f"   Confidence below 0.75 means keep looking.\n"
            f"4. PINPOINT — identify the exact wrong string/value (the 'buggy_pattern') so the fix is 1 line.\n\n"
            f"NEVER read _test files, swagger, or node_modules.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Repo: {ctx.repo_path}\n\n"
            f"Reproduction log:\n{ctx.reproduction_log[:3000]}\n\n"
            "Respond with JSON only:\n"
            '{"root_cause": "...", "confidence": 0.XX, '
            '"files": ["relative/path/to/file.go"], '
            '"buggy_pattern": "exact string that is wrong in the source"}'
        ),
    }]

    for _ in range(8):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=_ROOT_CAUSE_TOOLS,
            messages=messages,
        )
        tracker.record(response.model, response.usage)

        if response.stop_reason == "end_turn":
            _text = next((b.text for b in response.content if b.type == "text"), "")
            try:
                _extract_json(_text)
                break  # Valid JSON — done
            except Exception:
                pass  # Prose response — extract JSON via a fresh call with no tool history

            # Fresh call: no tool history so the model can't keep doing tool_use
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=512,
                messages=[{
                    "role": "user",
                    "content": (
                        "Convert this bug analysis to JSON only. "
                        "No prose, no markdown fences, no explanation — just the JSON object.\n\n"
                        f"Analysis:\n{_text[:3000]}\n\n"
                        "Output exactly this shape (fill in real values from the analysis above):\n"
                        '{"root_cause": "one-sentence description of the exact bug", '
                        '"confidence": 0.90, '
                        '"files": ["relative/path/from/repo/root.ts"], '
                        '"buggy_pattern": "exact wrong string or condition from the source code"}'
                    ),
                }],
            )
            tracker.record(response.model, response.usage)
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
    else:
        # Exhausted iterations — force a final JSON-only response with no tools
        messages.append({"role": "assistant", "content": response.content})
        # Exhausted iterations — summarise what we know into a fresh JSON-only call
        last_prose = next(
            (b.text for b in response.content if b.type == "text"), ""
        ) or "no text returned"
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": (
                    "Convert this bug analysis to JSON only. "
                    "No prose, no markdown fences, no explanation — just the JSON object.\n\n"
                    f"Analysis:\n{last_prose[:3000]}\n\n"
                    "Output exactly this shape (fill in real values from the analysis above):\n"
                    '{"root_cause": "one-sentence description of the exact bug", '
                    '"confidence": 0.90, '
                    '"files": ["relative/path/from/repo/root.ts"], '
                    '"buggy_pattern": "exact wrong string or condition from the source code"}'
                ),
            }],
        )
        tracker.record(response.model, response.usage)

    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise ValueError(f"Root cause analysis returned no text. Content types: {[b.type for b in response.content]}")
    print(f"[triage] root-cause response text (first 300 chars): {text[:300]!r}", flush=True)
    try:
        data = _extract_json(text)
    except Exception as e:
        raise ValueError(f"Root cause JSON parse failed: {e}\nRaw text: {text[:500]}")
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
    if name == "browser_evaluate":
        return browser_evaluate(inputs["script"])
    if name == "browser_press":
        return browser_press(inputs["selector"], inputs["key"])
    if name == "browser_go_back":
        return browser_go_back()
    return f"Unknown tool: {name}"
