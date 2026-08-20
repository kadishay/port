import threading
import time
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


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_records_telemetry_on_normal_completion(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    def fake_triage(ctx, gh, tracker):
        ctx.not_a_bug = True
        return ctx

    with patch("agent.orchestrator.run_triage", side_effect=fake_triage):
        result = run_pipeline(42)

    mock_record.assert_called_once()
    args, kwargs = mock_record.call_args
    assert args[0] is result
    assert kwargs["crashed"] is False
    assert isinstance(args[2], float)  # duration_seconds


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_records_telemetry_with_crashed_true_on_exception(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    with patch("agent.orchestrator.run_triage", side_effect=RuntimeError("boom")):
        run_pipeline(42)

    mock_record.assert_called_once()
    _, kwargs = mock_record.call_args
    assert kwargs["crashed"] is True


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_does_not_record_telemetry_when_pr_already_open(mock_record, mock_gh_class, mock_refresh):
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = {"html_url": "https://github.com/x/y/pull/5"}

    run_pipeline(42)

    mock_record.assert_not_called()


@patch("agent.orchestrator._refresh_vikunja_token")
@patch("agent.orchestrator.GitHubClient")
@patch("agent.orchestrator.record_run")
def test_run_pipeline_serializes_concurrent_runs(mock_record, mock_gh_class, mock_refresh):
    """Two overlapping run_pipeline() calls (simulating overlapping/retried webhook
    deliveries) must never have their working-tree-mutating sections (triage/solve)
    active at the same time — verified via a shared counter incremented/decremented
    around each mocked call."""
    from agent.orchestrator import run_pipeline

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh
    mock_gh.get_issue.return_value = {"title": "Bug", "body": "..."}
    mock_gh.get_open_pr_for_branch.return_value = None

    active = 0
    max_active = 0
    counter_lock = threading.Lock()

    def bump():
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1

    def fake_triage(ctx, gh, tracker):
        bump()
        return ctx

    def fake_solve(ctx, gh, tracker):
        bump()
        return ctx

    with patch("agent.orchestrator.run_triage", side_effect=fake_triage), \
         patch("agent.orchestrator.run_solve", side_effect=fake_solve):
        threads = [threading.Thread(target=run_pipeline, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

    assert max_active <= 1, "triage/solve ran concurrently across pipeline runs — lock not effective"
