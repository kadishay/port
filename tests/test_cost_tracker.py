from unittest.mock import MagicMock
from agent.cost_tracker import CostTracker


def test_models_used_returns_normalized_model_keys():
    tracker = CostTracker()
    usage = MagicMock(input_tokens=100, output_tokens=50)
    tracker.record("claude-haiku-4-5-20251001", usage)
    assert tracker.models_used() == {"claude-haiku-4-5"}


def test_models_used_includes_every_recorded_model():
    tracker = CostTracker()
    usage = MagicMock(input_tokens=10, output_tokens=10)
    tracker.record("claude-haiku-4-5-20251001", usage)
    tracker.record("claude-opus-4-8-20260101", usage)
    assert tracker.models_used() == {"claude-haiku-4-5", "claude-opus-4-8"}


def test_models_used_empty_when_nothing_recorded():
    tracker = CostTracker()
    assert tracker.models_used() == set()
