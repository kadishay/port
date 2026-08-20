import os
import threading
from pathlib import Path
import tempfile

# Thread-local browser session — each pipeline thread gets its own Playwright instance.
_local = threading.local()


def playwright_enabled() -> bool:
    return os.environ.get("PLAYWRIGHT_ENABLED", "false").lower() == "true"


def _get_page():
    from playwright.sync_api import sync_playwright
    if not getattr(_local, "page", None):
        _local.pw = sync_playwright().start()
        headless = os.environ.get("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        _local.browser = _local.pw.chromium.launch(headless=headless)
        _local.page = _local.browser.new_page()
    return _local.page


def browser_navigate(url: str) -> str:
    page = _get_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(800)
        return f"Navigated to {url} — title: {page.title()}"
    except Exception as e:
        return f"Navigation failed: {e}"


def browser_click(selector: str) -> str:
    page = _get_page()
    try:
        page.click(selector, timeout=8000)
        page.wait_for_timeout(600)
        return f"Clicked '{selector}'"
    except Exception as e:
        return f"Click failed for '{selector}': {e}"


def browser_type(selector: str, text: str) -> str:
    page = _get_page()
    try:
        page.fill(selector, text, timeout=5000)
        return f"Typed into '{selector}'"
    except Exception as e:
        return f"Type failed for '{selector}': {e}"


def browser_get_text(selector: str) -> str:
    page = _get_page()
    try:
        return page.inner_text(selector, timeout=5000)
    except Exception as e:
        return f"get_text failed for '{selector}': {e}"


def _screenshots_base_dir() -> Path:
    """Directory to root the vikunja-screenshots/ folder under.

    On Railway, `tempfile.gettempdir()` is the container's ephemeral
    filesystem — unreachable once the deployment is remote, since there's
    no laptop filesystem to browse afterward. When VIKUNJA_REPO_PATH is
    set (the Railway deployment), screenshots go under its *parent*
    directory instead: still on the same Railway-mounted persistent
    Volume (the volume is mounted at a directory that contains
    VIKUNJA_REPO_PATH), so they survive container restarts, but
    deliberately outside VIKUNJA_REPO_PATH itself — agent/solve.py runs
    `git add -A && git commit` inside VIKUNJA_REPO_PATH when proposing a
    fix, and screenshots written inside that tree would get swept into
    the fix commit and corrupt the PR diff. Falls back to
    tempfile.gettempdir() when VIKUNJA_REPO_PATH isn't set, preserving
    current local dev/test behavior unchanged.
    """
    repo_path = os.environ.get("VIKUNJA_REPO_PATH")
    if repo_path:
        return Path(repo_path).parent
    return Path(tempfile.gettempdir())


def browser_screenshot(filename: str) -> str:
    """Take a screenshot and save it. Returns the absolute path to the saved file."""
    page = _get_page()
    out_dir = _screenshots_base_dir() / "vikunja-screenshots"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / filename
    page.screenshot(path=str(path))
    return str(path)


def browser_settle(timeout_ms: int = 5000) -> None:
    """Force-close any open modal (Escape) and wait for in-flight network requests to
    finish. Used right before the automatic (code-driven) before/after screenshots so
    they don't capture a stuck-open task modal or a stale pre-fetch of the board —
    e.g. if 'mark done' hadn't finished persisting before go_back() re-fetched the
    board data. Not exposed as an LLM tool; called directly around screenshot capture."""
    page = _get_page()
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    page.wait_for_timeout(500)


def browser_wait(milliseconds: int = 1000) -> str:
    _get_page().wait_for_timeout(milliseconds)
    return f"Waited {milliseconds}ms"


def browser_press(selector: str, key: str) -> str:
    """Press a key on a focused element (e.g., 'Enter', 'Tab', 'Escape')."""
    page = _get_page()
    try:
        page.press(selector, key, timeout=5000)
        page.wait_for_timeout(200)
        return f"Pressed '{key}' on '{selector}'"
    except Exception as e:
        return f"Press failed for '{selector}' key '{key}': {e}"


def browser_evaluate(script: str) -> str:
    """Run a JavaScript expression in the browser page context."""
    page = _get_page()
    try:
        result = page.evaluate(script)
        return f"OK: {result}"
    except Exception as e:
        return f"Evaluate failed: {e}"


def browser_go_back() -> str:
    """Navigate back in browser history."""
    page = _get_page()
    try:
        page.go_back(wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(800)
        return f"Navigated back — now at: {page.url}"
    except Exception as e:
        return f"go_back failed: {e}"


def close_browser() -> None:
    if getattr(_local, "page", None):
        _local.browser.close()
        _local.pw.stop()
        _local.page = None
        _local.browser = None
        _local.pw = None


# Tool definitions for the Claude API
BROWSER_TOOLS = [
    {
        "name": "browser_navigate",
        "description": "Navigate the browser to a URL. Use for the Vikunja frontend at http://localhost:4173.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click an element by CSS selector or text selector (e.g. 'text=Mark as Done', 'button.done-btn').",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an input field identified by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_get_text",
        "description": "Get the visible text content of an element by CSS selector.",
        "input_schema": {
            "type": "object",
            "properties": {"selector": {"type": "string"}},
            "required": ["selector"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Take a screenshot of the current browser state and save it. Returns the file path.",
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string", "description": "e.g. 'bug-42-before.png'"}},
            "required": ["filename"],
        },
    },
    {
        "name": "browser_wait",
        "description": "Wait for UI animations or async updates to settle.",
        "input_schema": {
            "type": "object",
            "properties": {"milliseconds": {"type": "integer", "default": 1000}},
            "required": [],
        },
    },
    {
        "name": "browser_press",
        "description": "Press a keyboard key on an element (e.g. 'Enter' to submit a form). Use 'browser_press' with selector '#password' and key 'Enter' to submit the login form.",
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "key": {"type": "string", "description": "Key to press, e.g. 'Enter', 'Tab', 'Escape'"},
            },
            "required": ["selector", "key"],
        },
    },
    {
        "name": "browser_evaluate",
        "description": "Run a JavaScript expression in the browser page. Use to set localStorage, read DOM values, etc. E.g. 'localStorage.setItem(\"API_URL\", \"http://localhost:3456\")'",
        "input_schema": {
            "type": "object",
            "properties": {"script": {"type": "string"}},
            "required": ["script"],
        },
    },
    {
        "name": "browser_go_back",
        "description": "Navigate back in browser history (like pressing the browser Back button).",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
