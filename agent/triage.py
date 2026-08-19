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
    browser_press, browser_go_back, browser_settle, close_browser, playwright_enabled,
)
from agent.cost_tracker import CostTracker

client = anthropic.Anthropic()

_HAIKU = "claude-haiku-4-5"
_OPUS = "claude-opus-4-8"
_OPUS_RETRY_CONFIDENCE_THRESHOLD = 0.70
_REPRODUCTION_AUTO_CLOSE_THRESHOLD = 0.85


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

    print(f"[triage] #{ctx.issue_number} — checking if bug reproduced", flush=True)
    reproduced, repro_confidence, repro_reason = _check_reproduction(ctx, tracker)
    ctx.reproduction_confidence = repro_confidence
    ctx.reproduction_reason = repro_reason

    if not reproduced:
        ctx.unable_to_reproduce = True
        print(
            f"[triage] #{ctx.issue_number} — unable to reproduce "
            f"(confidence={repro_confidence:.2f}): {repro_reason}",
            flush=True,
        )
        gh.post_comment(ctx.issue_number, _unable_to_reproduce_comment(ctx))
        gh.add_label(ctx.issue_number, "cannot-reproduce")
        if repro_confidence >= _REPRODUCTION_AUTO_CLOSE_THRESHOLD:
            gh.close_issue(ctx.issue_number)
            ctx.issue_closed = True
            print(
                f"[triage] #{ctx.issue_number} — auto-closed "
                f"(confidence ≥ {_REPRODUCTION_AUTO_CLOSE_THRESHOLD})",
                flush=True,
            )
        return ctx

    _check_not_a_bug(ctx)
    if ctx.not_a_bug:
        _post_not_a_bug_comment(ctx, gh)
        return ctx

    import time as _time
    print(f"[triage] #{ctx.issue_number} — root cause analysis", flush=True)
    _t0 = _time.time()
    ctx.root_cause, ctx.confidence, ctx.affected_files, ctx.buggy_pattern = _analyze_root_cause(ctx, tracker)
    print(f"[triage] #{ctx.issue_number} — root cause done in {_time.time() - _t0:.1f}s (confidence={ctx.confidence:.2f})", flush=True)

    if ctx.confidence < _OPUS_RETRY_CONFIDENCE_THRESHOLD:
        print(
            f"[triage] #{ctx.issue_number} — confidence {ctx.confidence:.2f} < "
            f"{_OPUS_RETRY_CONFIDENCE_THRESHOLD} — retrying root cause with Opus",
            flush=True,
        )
        _t1 = _time.time()
        retry_note = (
            f"A faster model already analyzed this and reported low confidence "
            f"({ctx.confidence:.2f}): root_cause={ctx.root_cause!r}, files={ctx.affected_files}, "
            f"buggy_pattern={ctx.buggy_pattern!r}. Verify this or dig deeper — read more files if "
            f"needed — and report a more confident answer.\n\n"
        )
        ctx.root_cause, ctx.confidence, ctx.affected_files, ctx.buggy_pattern = _analyze_root_cause(
            ctx, tracker, model=_OPUS, retry_note=retry_note
        )
        print(
            f"[triage] #{ctx.issue_number} — Opus retry done in {_time.time() - _t1:.1f}s "
            f"(confidence={ctx.confidence:.2f})",
            flush=True,
        )

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

    max_tool_calls = 25 if use_browser else 4
    kanban_hint = (
        f"\nKANBAN STEPS — call these tools IN THIS EXACT ORDER, one per tool call:\n"
        f"1) browser_navigate 'http://localhost:4173'\n"
        f"2) browser_evaluate 'localStorage.setItem(\"API_URL\",\"http://localhost:3456\")'\n"
        f"3) browser_navigate 'http://localhost:4173'\n"
        f"4) browser_wait 1000\n"
        f"5) browser_type '#username' '{vikunja_username}'\n"
        f"6) browser_type '#password' '{vikunja_password}'\n"
        f"7) browser_press '#password' 'Enter'\n"
        f"8) browser_wait 2000\n"
        f"9) run_shell: TASK_ID=$(curl -s -X PUT http://localhost:3456/api/v1/projects/3/tasks "
        f"{auth_header} -H 'Content-Type: application/json' -d '{{\"title\":\"Kanban Repro Task\"}}' "
        f"| python3 -c \"import sys,json;print(json.load(sys.stdin)['id'])\") && "
        f"curl -s -X POST http://localhost:3456/api/v1/projects/3/views/20/buckets/13/tasks "
        f"{auth_header} -H 'Content-Type: application/json' -d \"{{\\\"task_id\\\":$TASK_ID}}\"  "
        f"(creates a FRESH task and moves it into the To-Do bucket — do NOT reuse an existing "
        f"card; repeated test runs may have already marked all existing tasks done, so there "
        f"may be no undone task left to click)\n"
        f"10) browser_navigate 'http://localhost:4173/projects/3/20'\n"
        f"11) browser_wait 3000\n"
        f"12) browser_click 'text=Kanban Repro Task'\n"
        f"13) browser_wait 2000\n"
        f"14) browser_click '.button--mark-done'\n"
        f"15) browser_wait 2000\n"
        f"16) browser_go_back\n"
        f"17) browser_wait 3000\n"
        f"After step 17, STOP immediately — do NOT call browser_screenshot. "
        f"A screenshot will be taken automatically. Summarize what you observed.\n"
        if (use_browser and "kanban" in ctx.issue_title.lower()) else ""
    )
    color_hint = (
        f"\nCOLOR STEPS — call these browser tools IN THIS EXACT ORDER, one per tool call:\n"
        f"1) browser_navigate 'http://localhost:4173'\n"
        f"2) browser_evaluate 'localStorage.setItem(\"API_URL\",\"http://localhost:3456\")'\n"
        f"3) browser_navigate 'http://localhost:4173'\n"
        f"4) browser_wait 1000\n"
        f"5) browser_type '#username' '{vikunja_username}'\n"
        f"6) browser_type '#password' '{vikunja_password}'\n"
        f"7) browser_press '#password' 'Enter'\n"
        f"8) browser_wait 2000\n"
        f"9) browser_navigate 'http://localhost:4173/projects/3'\n"
        f"10) browser_wait 3000\n"
        f"11) browser_click '.task-link'  (opens the first task)\n"
        f"12) browser_wait 2000\n"
        f"13) browser_click 'text=Set Color'  (reveals the color picker field)\n"
        f"14) browser_wait 1000\n"
        f"15) browser_type '.picker__input' '#ff0000'  (sets the color — this is a native "
        f"<input type=\"color\">, so browser_type/fill sets the value directly; do NOT try to "
        f"click a swatch or circle, there isn't a clickable one in the DOM)\n"
        f"16) browser_wait 2000  (color picker auto-saves on change, no Save button to click)\n"
        f"17) browser_go_back\n"
        f"18) browser_wait 3000\n"
        f"After step 18, STOP immediately — do NOT call browser_screenshot. "
        f"A screenshot will be taken automatically. Summarize what you observed.\n"
        if (use_browser and "color" in ctx.issue_title.lower()) else ""
    )
    ui_hint = kanban_hint or color_hint

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
            "- Do NOT create new tasks or users, UNLESS a numbered script below explicitly tells you to.\n"
            "- macOS date syntax: use $(date -v+1d +%s) for 'tomorrow', NOT date -d.\n"
            + (
                f"- This is a FRONTEND bug. Search {ctx.repo_path}/frontend/src/ for .ts/.vue files. "
                f"Do NOT read .go files. "
                + (f"{login_instruction}Use browser_* tools to reproduce visually. "
                   f"{ui_hint}"
                   f"Take a final browser_screenshot named 'bug-{ctx.issue_number}-before.png' showing the bug state.\n"
                   if use_browser else
                   f"Run: find {ctx.repo_path}/frontend/src/stores -name '*.ts' | head -10 "
                   f"to list store files, then read_file the most relevant one. "
                   f"After reading, also run: grep -n 'Bucket\\|bucket\\|done' <that-file> | head -40 "
                   f"to highlight the condition-heavy lines.\n")
                if is_ui_bug else
                "- This is a BACKEND bug. Use run_shell (curl with auth header) AND read_file on the "
                "most relevant source file in pkg/models/. Do NOT use browser tools.\n"
            ) +
            f"- Stop after {max_tool_calls} tool calls. Summarize what you observed."
        ),
    }]
    log_parts: list[str] = []
    max_iterations = 25 if use_browser else 5

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

    # Take the "before fix" screenshot programmatically so the name is always canonical
    # and it always captures the current browser state (kanban board after go_back).
    if use_browser:
        browser_settle()
        canonical = f"bug-{ctx.issue_number}-before.png"
        path = browser_screenshot(canonical)
        if not path.startswith("Error"):
            ctx.screenshot_before = path
            print(f"[triage] before-screenshot: {path}", flush=True)

    close_browser()
    return "\n".join(log_parts)


_REPRODUCTION_TOOL = {
    "name": "report_reproduction",
    "description": "Report whether there is real evidence the bug described in the issue exists.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reproduced": {
                "type": "boolean",
                "description": (
                    "true if you either (a) directly observed the 'Actual behaviour' happening, or "
                    "(b) found a SPECIFIC, concrete logic flaw — an identifiable wrong condition, "
                    "operator, or value on a particular line — that would cause it. Vague suspicion "
                    "('might not be wired up', 'doesn't explicitly show X', 'unclear if Y persists') "
                    "is NOT evidence; it's speculation from an incomplete search, not a finding. "
                    "false if EITHER: you found the exact relevant code path and it correctly matches "
                    "'Expected behaviour' with no identifiable flaw, OR the feature clearly has a "
                    "complete, intentional-looking implementation (the UI component, the data field, "
                    "and the rendering logic all exist and connect to each other) and you cannot point "
                    "to one specific broken line — a complete implementation with no identifiable defect "
                    "is evidence the feature works, you don't need airtight proof of correctness."
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "0.0–1.0. For reproduced=false, this is confidence that the code is CORRECT — "
                    "not confidence that the search was thorough."
                ),
            },
            "reason": {"type": "string", "description": "One or two sentences: what was observed and why"},
        },
        "required": ["reproduced", "confidence", "reason"],
    },
}


def _check_reproduction(ctx: BugContext, tracker: CostTracker) -> tuple[bool, float, str]:
    """Ask Haiku whether the bug actually exists. Gets a few extra tool calls to dig further
    if the reproduction log alone is inconclusive, instead of judging blind on whatever the
    (budget-limited) reproduce step happened to find."""
    messages = [{
        "role": "user",
        "content": (
            f"Issue: {ctx.issue_title}\n{ctx.issue_body}\n\n"
            f"Reproduction attempt log so far:\n{ctx.reproduction_log[:8000]}\n\n"
            f"Repo: {ctx.repo_path}\n\n"
            "Is there real evidence this bug exists? A specific, concrete logic flaw found by reading "
            "code counts as evidence — you do NOT need to have triggered it live. If the log above is "
            "inconclusive, use up to 5 more tool calls (read_file/run_shell) before deciding. Search "
            "broadly first — e.g. `grep -rl '<relevant field/keyword>' <repo>/frontend/src/components` "
            "— there are often several similarly-named components (list view, detail view, card view, "
            "readonly view); check more than one before concluding you can't find the relevant code. "
            "Only report reproduced=false once you've actually found and read the specific code path "
            "the reproduction steps exercise, not an adjacent one. Call report_reproduction with your "
            "verdict when ready."
        ),
    }]

    for _ in range(6):
        response = client.messages.create(
            model=_HAIKU,
            max_tokens=2048,
            tools=[*_TRIAGE_TOOLS, _REPRODUCTION_TOOL],
            messages=messages,
        )
        tracker.record(response.model, response.usage)

        for block in response.content:
            if block.type == "tool_use" and block.name == "report_reproduction":
                d = block.input
                return bool(d.get("reproduced", True)), float(d.get("confidence", 0.5)), d.get("reason", "")

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

    return _force_reproduction_verdict(ctx, tracker, messages)


def _force_reproduction_verdict(
    ctx: BugContext, tracker: CostTracker, messages: list,
) -> tuple[bool, float, str]:
    """Forced verdict when the loop ran out of iterations — reuses the full investigation
    transcript (everything read so far) as context, not just the issue text, so this isn't
    a guess made from nothing."""
    closing = (
        "You've run out of tool calls. Based on everything you've read above (not on how the "
        "issue *sounds*), is there real evidence this bug exists? Call report_reproduction with "
        "your best-effort verdict."
    )
    # The last message here is always role="user" (either the initial prompt, if the model
    # hit end_turn immediately, or the final tool_results) — the API requires strict
    # user/assistant alternation, so the closing instruction must be merged into it rather
    # than appended as a new message.
    last = messages[-1]
    if last["role"] == "user":
        content = last["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        messages = messages[:-1] + [{"role": "user", "content": [*content, {"type": "text", "text": closing}]}]
    else:
        messages = messages + [{"role": "user", "content": closing}]
    response = client.messages.create(
        model=_HAIKU,
        max_tokens=512,
        tools=[_REPRODUCTION_TOOL],
        tool_choice={"type": "tool", "name": "report_reproduction"},
        messages=messages,
    )
    tracker.record(response.model, response.usage)
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_reproduction":
            d = block.input
            return bool(d.get("reproduced", True)), float(d.get("confidence", 0.5)), d.get("reason", "")
    # Fail safe: if the model returned no verdict, assume reproduced so the pipeline
    # doesn't silently skip a real bug.
    return True, 0.0, "reproduction-check returned no verdict"


def _unable_to_reproduce_comment(ctx: BugContext) -> str:
    auto_closed = ctx.reproduction_confidence >= _REPRODUCTION_AUTO_CLOSE_THRESHOLD
    closed_note = (
        "🔒 **Closing automatically** — confidence is high enough that this doesn't need human review. "
        "Reopen if you can provide more detail or a clearer repro."
        if auto_closed else
        "⚠️ **Leaving this open** — confidence isn't high enough to auto-close. A human should take a look."
    )
    return (
        f"## 🔍 Unable to Reproduce — #{ctx.issue_number}\n\n"
        f"I followed the reproduction steps but did not observe the described bug.\n\n"
        f"**Confidence:** {ctx.reproduction_confidence:.0%}\n"
        f"**What I observed:** {ctx.reproduction_reason}\n\n"
        f"{closed_note}\n\n"
        f"*Triage powered by Claude Haiku 4.5*"
    )


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


_REPORT_TOOL = {
    "name": "report_root_cause",
    "description": "Report your root cause findings as structured data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "root_cause":    {"type": "string", "description": "One sentence: what is wrong and why"},
            "confidence":    {"type": "number", "description": "0.0–1.0 confidence score"},
            "files":         {"type": "array", "items": {"type": "string"}, "description": "Affected file paths"},
            "buggy_pattern": {"type": "string", "description": "Exact wrong token/operator/value on a SINGLE LINE — short enough to grep for (≤80 chars). Example: '=== currentView.doneBucketId' or 'And(\"done = true\")'. No newlines."},
        },
        "required": ["root_cause", "confidence", "files", "buggy_pattern"],
    },
}


def _force_tool_extraction(prose: str, ctx: BugContext, tracker: CostTracker, model: str = _HAIKU) -> tuple[str, float, list[str], str]:
    """Extract structured root-cause data by forcing a tool call — model cannot return prose."""
    content = f"Issue: {ctx.issue_title}\n\nYour analysis so far:\n{prose[:3000]}\n\nCall report_root_cause with your findings."
    response = client.messages.create(
        model=model,
        max_tokens=512,
        tools=[_REPORT_TOOL],
        tool_choice={"type": "tool", "name": "report_root_cause"},
        messages=[{"role": "user", "content": content}],
    )
    tracker.record(response.model, response.usage)
    for block in response.content:
        if block.type == "tool_use" and block.name == "report_root_cause":
            d = block.input
            return (
                d.get("root_cause", "Unknown"),
                float(d.get("confidence", 0.5)),
                d.get("files", []),
                d.get("buggy_pattern", ""),
            )
    raise ValueError("Forced tool extraction returned no report_root_cause call")


def _analyze_root_cause(
    ctx: BugContext, tracker: CostTracker, model: str = _HAIKU, retry_note: str = "",
) -> tuple[str, float, list[str], str]:
    is_fe = _is_frontend_bug(ctx.issue_title)
    search_dir = f"{ctx.repo_path}/frontend/src" if is_fe else f"{ctx.repo_path}/pkg/models"
    file_ext = "*.ts or *.vue" if is_fe else "*.go"
    bug_type = "FRONTEND (TypeScript/Vue)" if is_fe else "BACKEND (Go)"

    messages = [{
        "role": "user",
        "content": (
            f"{retry_note}"
            f"Find the root cause of this Vikunja bug. Use up to 6 tool calls. Work systematically:\n\n"
            f"BUG TYPE: {bug_type}\n"
            f"SEARCH DIRECTORY: {search_dir}\n"
            f"FILE TYPES: {file_ext} ONLY — do NOT read files of other types.\n\n"
            f"IMPORTANT: If the reproduction log below already contains the buggy source code, "
            f"call report_root_cause IMMEDIATELY — no tool calls needed. "
            f"The reproduction log may contain file contents from the reproduce phase.\n\n"
            f"SEARCH STRATEGY (only if log is insufficient):\n"
            f"1. FILENAME FIRST — find files whose NAME contains a keyword from the issue title:\n"
            f"   find {search_dir} -name '*<keyword>*' | grep -v test | grep -v node_modules\n"
            f"2. READ THE FILE — read_file the best candidate. Look for the specific wrong value/condition.\n"
            f"3. IF UNCERTAIN — if you can't spot the bug in the first file, search for one more.\n"
            f"   Confidence below 0.75 means keep looking.\n"
            f"4. PINPOINT — identify the exact wrong string/value (the 'buggy_pattern') so the fix is 1 line.\n\n"
            f"NEVER read _test files, swagger, or node_modules.\n\n"
            f"Issue: {ctx.issue_title}\n"
            f"Repo: {ctx.repo_path}\n\n"
            f"Reproduction log (may contain source file contents):\n{ctx.reproduction_log[:8000]}\n\n"
            "When done, call report_root_cause with your findings."
        ),
    }]

    last_prose = ""

    for _ in range(8):
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            tools=[*_ROOT_CAUSE_TOOLS, _REPORT_TOOL],
            messages=messages,
        )
        tracker.record(response.model, response.usage)

        # Check for the structured report tool — this is the happy path
        for block in response.content:
            if block.type == "tool_use" and block.name == "report_root_cause":
                d = block.input
                print(f"[triage] root-cause via report_root_cause tool [{model}] (confidence={d.get('confidence')})", flush=True)
                return (
                    d.get("root_cause", "Unknown"),
                    float(d.get("confidence", 0.5)),
                    d.get("files", []),
                    d.get("buggy_pattern", ""),
                )

        if response.stop_reason == "end_turn":
            # Model wrote prose instead of calling the tool — try JSON parse as a courtesy
            _text = next((b.text for b in response.content if b.type == "text"), "")
            last_prose = _text or last_prose
            print(f"[triage] end_turn prose (first 200): {_text[:200]!r}", flush=True)
            try:
                data = _extract_json(_text)
                print("[triage] root-cause extracted from inline JSON", flush=True)
                return (
                    data["root_cause"],
                    float(data["confidence"]),
                    data.get("files", []),
                    data.get("buggy_pattern", ""),
                )
            except Exception:
                break  # fall through to forced tool extraction

        tool_results = []
        for block in response.content:
            if block.type == "tool_use" and block.name != "report_root_cause":
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
        last_prose = next((b.text for b in response.content if b.type == "text"), "") or last_prose

    print(f"[triage] falling back to forced tool extraction [{model}] (prose len={len(last_prose)})", flush=True)
    return _force_tool_extraction(last_prose, ctx, tracker, model=model)


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
