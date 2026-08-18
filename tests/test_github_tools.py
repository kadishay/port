import responses as resp_mock
import pytest
from agent.tools.github_tools import GitHubClient

REPO = "test-owner/test-repo"


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", REPO)


@resp_mock.activate
def test_get_issue():
    resp_mock.add(
        resp_mock.GET,
        f"https://api.github.com/repos/{REPO}/issues/42",
        json={"number": 42, "title": "Bug: tasks overdue"},
        status=200,
    )
    client = GitHubClient()
    issue = client.get_issue(42)
    assert issue["number"] == 42


@resp_mock.activate
def test_post_comment():
    resp_mock.add(
        resp_mock.POST,
        f"https://api.github.com/repos/{REPO}/issues/42/comments",
        json={"id": 1, "body": "triage done"},
        status=201,
    )
    client = GitHubClient()
    result = client.post_comment(42, "triage done")
    assert result["id"] == 1


@resp_mock.activate
def test_get_comments_empty():
    resp_mock.add(
        resp_mock.GET,
        f"https://api.github.com/repos/{REPO}/issues/42/comments",
        json=[],
        status=200,
    )
    client = GitHubClient()
    comments = client.get_comments(42)
    assert comments == []


@resp_mock.activate
def test_create_pr():
    resp_mock.add(
        resp_mock.POST,
        f"https://api.github.com/repos/{REPO}/pulls",
        json={"number": 7, "html_url": "https://github.com/test-owner/test-repo/pull/7"},
        status=201,
    )
    client = GitHubClient()
    pr = client.create_pr("fix: overdue window", "fixes #42", "fix/issue-42")
    assert pr["number"] == 7
