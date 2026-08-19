from unittest.mock import patch, MagicMock
from agent.models import BugContext, Severity
from agent.cost_tracker import CostTracker


def _end_turn(text: str):
    block = MagicMock()
    block.type = "text"
    block.text = text
    r = MagicMock()
    r.stop_reason = "end_turn"
    r.content = [block]
    return r


def _tool_use(name: str, input_dict: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = input_dict
    block.id = "tool_1"
    r = MagicMock()
    r.stop_reason = "tool_use"
    r.content = [block]
    r.model = "claude-haiku-4-5"
    return r


@patch("agent.triage._find_relevant_people")
@patch("agent.triage.GitHubClient")
@patch("agent.triage.client")
def test_triage_sets_severity_high(mock_client, mock_gh_class, mock_people, tmp_path):
    from agent.triage import run_triage

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh

    mock_client.messages.create.side_effect = [
        _end_turn("Run: curl http://localhost:3456/api/v1/tasks"),
        _end_turn("Reproduction complete. Output: tasks due tomorrow flagged overdue."),
        _tool_use("report_reproduction", {
            "reproduced": True, "confidence": 0.9, "reason": "Observed the overdue flag firing as described.",
        }),
        _end_turn('{"root_cause": "time.Hour*38 window too large", "confidence": 0.92, '
                  '"files": ["pkg/models/task_overdue_reminder.go"], "buggy_pattern": "time.Hour*38"}'),
        _end_turn('{"severity": "HIGH"}'),
    ]

    ctx = BugContext(
        issue_number=42,
        issue_title="Tasks due tomorrow shown as overdue",
        issue_body="## Steps\n1. Create task due tomorrow\n2. Check overdue list",
        repo_path=str(tmp_path),
    )

    result = run_triage(ctx, mock_gh, CostTracker())

    assert result.severity == Severity.HIGH
    assert result.confidence == 0.92
    assert "time.Hour*38" in result.root_cause
    assert result.reproduction_log != ""
    mock_gh.post_comment.assert_called_once()


@patch("agent.triage._find_relevant_people")
@patch("agent.triage.GitHubClient")
@patch("agent.triage.client")
def test_root_cause_low_confidence_retries_with_opus(mock_client, mock_gh_class, mock_people, tmp_path):
    from agent.triage import run_triage

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh

    mock_client.messages.create.side_effect = [
        _end_turn("Run: curl http://localhost:3456/api/v1/tasks"),
        _end_turn("Reproduction complete. Output: tasks due tomorrow flagged overdue."),
        _tool_use("report_reproduction", {
            "reproduced": True, "confidence": 0.9, "reason": "Observed the overdue flag firing as described.",
        }),
        _tool_use("report_root_cause", {
            "root_cause": "Not sure, maybe the time window", "confidence": 0.5,
            "files": ["<UNKNOWN>"], "buggy_pattern": "",
        }),
        _tool_use("report_root_cause", {
            "root_cause": "time.Hour*38 window too large", "confidence": 0.95,
            "files": ["pkg/models/task_overdue_reminder.go"], "buggy_pattern": "time.Hour*38",
        }),
        _end_turn('{"severity": "HIGH"}'),
    ]

    ctx = BugContext(
        issue_number=42,
        issue_title="Tasks due tomorrow shown as overdue",
        issue_body="## Steps\n1. Create task due tomorrow\n2. Check overdue list",
        repo_path=str(tmp_path),
    )

    result = run_triage(ctx, mock_gh, CostTracker())

    assert result.confidence == 0.95
    assert "time.Hour*38" in result.root_cause
    calls = mock_client.messages.create.call_args_list
    assert calls[3].kwargs["model"] == "claude-haiku-4-5"
    assert calls[4].kwargs["model"] == "claude-opus-4-8"


@patch("agent.triage._find_relevant_people")
@patch("agent.triage.GitHubClient")
@patch("agent.triage.client")
def test_unable_to_reproduce_high_confidence_closes_issue(mock_client, mock_gh_class, mock_people, tmp_path):
    from agent.triage import run_triage

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh

    mock_client.messages.create.side_effect = [
        _end_turn("Run: read_file kanban.ts"),
        _end_turn("Read the color-setting code; it correctly persists and renders the color dot."),
        _tool_use("report_reproduction", {
            "reproduced": False, "confidence": 0.9,
            "reason": "The color-setting code path works correctly; no defect found.",
        }),
    ]

    ctx = BugContext(
        issue_number=99,
        issue_title="Bug: Set color is not working",
        issue_body="Steps: set a task color to red, expect a red dot, no dot appears.",
        repo_path=str(tmp_path),
    )

    result = run_triage(ctx, mock_gh, CostTracker())

    assert result.unable_to_reproduce is True
    assert result.issue_closed is True
    assert result.root_cause == ""  # never reached root cause
    mock_gh.close_issue.assert_called_once_with(99)
    mock_gh.add_label.assert_called_once_with(99, "cannot-reproduce")
    mock_gh.post_comment.assert_called_once()


@patch("agent.triage._find_relevant_people")
@patch("agent.triage.GitHubClient")
@patch("agent.triage.client")
def test_unable_to_reproduce_low_confidence_leaves_issue_open(mock_client, mock_gh_class, mock_people, tmp_path):
    from agent.triage import run_triage

    mock_gh = MagicMock()
    mock_gh_class.return_value = mock_gh

    mock_client.messages.create.side_effect = [
        _end_turn("Run: read_file kanban.ts"),
        _end_turn("Couldn't fully confirm either way from static code alone."),
        _tool_use("report_reproduction", {
            "reproduced": False, "confidence": 0.55,
            "reason": "Code looks correct but I couldn't fully verify without running the UI.",
        }),
    ]

    ctx = BugContext(
        issue_number=100,
        issue_title="Bug: Set color is not working",
        issue_body="Steps: set a task color to red, expect a red dot, no dot appears.",
        repo_path=str(tmp_path),
    )

    result = run_triage(ctx, mock_gh, CostTracker())

    assert result.unable_to_reproduce is True
    assert result.issue_closed is False
    mock_gh.close_issue.assert_not_called()
    mock_gh.add_label.assert_called_once_with(100, "cannot-reproduce")
    mock_gh.post_comment.assert_called_once()
