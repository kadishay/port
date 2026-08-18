import hashlib
import hmac
import json
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def app_env(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_REPO", "owner/repo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("VIKUNJA_REPO_PATH", "/tmp")


@pytest.fixture()
def client(app_env):
    from agent.webhook_server import app
    return TestClient(app)


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_webhook_returns_202(client, monkeypatch):
    monkeypatch.setattr("agent.webhook_server.enqueue_issue", lambda n: None)
    payload = json.dumps({"action": "opened", "issue": {"number": 42}}).encode()
    sig = _sign(payload, "test-secret")
    r = client.post(
        "/webhook",
        content=payload,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"},
    )
    assert r.status_code == 202


def test_invalid_signature_returns_403(client):
    payload = json.dumps({"action": "opened", "issue": {"number": 42}}).encode()
    r = client.post(
        "/webhook",
        content=payload,
        headers={"X-Hub-Signature-256": "sha256=bad", "X-GitHub-Event": "issues"},
    )
    assert r.status_code == 403


def test_non_opened_action_returns_200_no_enqueue(client, monkeypatch):
    enqueued = []
    monkeypatch.setattr("agent.webhook_server.enqueue_issue", lambda n: enqueued.append(n))
    payload = json.dumps({"action": "edited", "issue": {"number": 42}}).encode()
    sig = _sign(payload, "test-secret")
    r = client.post(
        "/webhook",
        content=payload,
        headers={"X-Hub-Signature-256": sig, "X-GitHub-Event": "issues"},
    )
    assert r.status_code == 200
    assert enqueued == []
