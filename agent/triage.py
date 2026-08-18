import json
import os
import anthropic
from agent.models import BugContext, Severity
from agent.tools.github_tools import GitHubClient
from agent.tools.shell_tools import run_shell
from agent.tools.file_tools import read_file

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


def run_triage(ctx: BugContext, gh: GitHubClient) -> BugContext:
    steps = _parse_reproduction_steps(ctx)
    ctx.reproduction_log = _reproduce(ctx, steps)

    _check_not_a_bug(ctx)
    if ctx.not_a_bug:
        _post_not_a_bug_comment(ctx, gh)
        return ctx

    ctx.root_cause, ctx.confidence = _analyze_root_cause(ctx)
    ctx.severity = _classify_severity(ctx)
    _post_triage_comment(ctx, gh)
    return ctx


def _parse_reproduction_steps(ctx: BugContext) -> str:
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": (
                f"Extract the exact shell/curl commands needed to reproduce this bug.\n\n"
                f"Issue: {ctx.issue_title}\n\n{ctx.issue_body}\n\n"
                "List only the commands, one per line. Assume Vikunja API is at "
                f"{os.environ.get('VIKUNJA_API_BASE', 'http://localhost:3456')}."
            ),
        }],
    )
    return next(b.text for b in response.content if b.type == "text")


def _reproduce(ctx: BugContext, steps: str) -> str:
    messages = [{
        "role": "user",
        "content": (
            f"Reproduce this bug by running these steps against the Vikunja instance.\n\n"
            f"Steps:\n{steps}\n\n"
            f"Vikunja repo: {ctx.repo_path}\n"
            "Capture all output. When done, summarize what you observed."
        ),
    }]
    log_parts: list[str] = []

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=4096,
            tools=_TRIAGE_TOOLS,
            messages=messages,
        )

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


def _analyze_root_cause(ctx: BugContext) -> tuple[str, float]:
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
                'Respond with JSON only: {"root_cause": "...", "confidence": 0.XX}'
            ),
        }],
    )
    text = next(b.text for b in response.content if b.type == "text")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(cleaned)
    return data["root_cause"], float(data["confidence"])


def _classify_severity(ctx: BugContext) -> Severity:
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
    text = next(b.text for b in response.content if b.type == "text")
    cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(cleaned)
    return Severity(data["severity"])


def _post_triage_comment(ctx: BugContext, gh: GitHubClient) -> None:
    severity_emoji = {"CRITICAL": "🔴", "HIGH": "🟠", "MEDIUM": "🟡", "LOW": "🟢"}
    body = (
        f"## 🤖 Automated Triage Report\n\n"
        f"**Severity:** {severity_emoji.get(ctx.severity, '⚪')} {ctx.severity}\n"
        f"**Confidence:** {ctx.confidence:.0%}\n\n"
        f"### Root Cause\n{ctx.root_cause}\n\n"
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
        except FileNotFoundError:
            return f"File not found: {inputs['path']}"
    return f"Unknown tool: {name}"
