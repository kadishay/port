import tempfile
from pathlib import Path

from agent.tools.browser_tools import _screenshots_base_dir


def test_screenshots_base_dir_uses_repo_path_parent_when_set(monkeypatch, tmp_path):
    """When VIKUNJA_REPO_PATH is set (the Railway deployment), screenshots must go
    under its *parent* directory — never inside VIKUNJA_REPO_PATH itself, since
    agent/solve.py runs `git add -A && git commit` inside that tree and screenshots
    written inside it would get swept into the fix commit and corrupt the PR diff."""
    repo_path = tmp_path / "data" / "vikunja"
    repo_path.mkdir(parents=True)
    monkeypatch.setenv("VIKUNJA_REPO_PATH", str(repo_path))

    base_dir = _screenshots_base_dir()

    assert base_dir == repo_path.parent
    assert base_dir != repo_path
    assert repo_path not in base_dir.parents  # base_dir is not nested inside repo_path


def test_screenshots_base_dir_falls_back_to_tempdir_when_unset(monkeypatch):
    """Preserves current local dev/test behavior unchanged when VIKUNJA_REPO_PATH
    isn't set."""
    monkeypatch.delenv("VIKUNJA_REPO_PATH", raising=False)

    base_dir = _screenshots_base_dir()

    assert base_dir == Path(tempfile.gettempdir())
