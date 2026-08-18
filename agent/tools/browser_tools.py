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
        _local.browser = _local.pw.chromium.launch(headless=True)
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


def browser_screenshot(filename: str) -> str:
    """Take a screenshot and save it. Returns the absolute path to the saved file."""
    page = _get_page()
    out_dir = Path(tempfile.gettempdir()) / "vikunja-screenshots"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / filename
    page.screenshot(path=str(path))
    return str(path)


def browser_wait(milliseconds: int = 1000) -> str:
    _get_page().wait_for_timeout(milliseconds)
    return f"Waited {milliseconds}ms"


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
]
