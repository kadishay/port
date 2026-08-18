import pytest
from unittest.mock import patch, MagicMock
from agent.hitl import wait_for_approval, HITLTimeout
from agent.models import BugContext


def _ctx():
    return BugContext(issue_number=42, issue_title="Bug", issue_body="...", repo_path="/tmp")


def _mock_gh(comments: list[dict]):
    gh = MagicMock()
    gh.get_comments.return_value = comments
    return gh


def test_returns_true_on_approve():
    gh = _mock_gh([{"body": "/approve", "created_at": "2026-01-01T00:01:00Z"}])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)
    assert result is True


def test_returns_false_on_reject():
    gh = _mock_gh([{"body": "/reject", "created_at": "2026-01-01T00:01:00Z"}])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)
    assert result is False


def test_raises_on_timeout():
    gh = _mock_gh([])
    with patch("agent.hitl.time.sleep"), patch("agent.hitl.time.time", side_effect=[0, 0, 999]):
        with pytest.raises(HITLTimeout):
            wait_for_approval(_ctx(), gh, timeout_seconds=10, poll_interval=1)


def test_ignores_comments_without_command():
    gh = _mock_gh([
        {"body": "Looks like a real bug", "created_at": "2026-01-01T00:01:00Z"},
        {"body": "/approve", "created_at": "2026-01-01T00:02:00Z"},
    ])
    with patch("agent.hitl.time.sleep"):
        result = wait_for_approval(_ctx(), gh, timeout_seconds=30, poll_interval=1)
    assert result is True
