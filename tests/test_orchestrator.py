from unittest.mock import patch, MagicMock


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
def test_skips_when_open_pr_exists(mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = {"html_url": "https://github.com/x/y/pull/5"}

    with patch("agent.orchestrator.run_triage") as mock_triage, \
         patch("agent.orchestrator.run_solve") as mock_solve:
        result = run_pipeline(42)

    assert result is None
    mock_triage.assert_not_called()
    mock_solve.assert_not_called()
    mock_gh.get_open_pr_for_branch.assert_called_once_with("fix/issue-42")
    mock_gh.post_comment.assert_called_once()
    assert "already has an open PR" in mock_gh.post_comment.call_args[0][1]


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
def test_posts_error_comment_on_exception(mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    with patch("agent.orchestrator.run_triage", side_effect=RuntimeError("boom")):
        result = run_pipeline(42)

    assert result is not None
    error_calls = [c for c in mock_gh.post_comment.call_args_list if "Pipeline crashed" in c[0][1]]
    assert error_calls, "expected an error comment to be posted"
    assert "boom" in error_calls[0][0][1]
