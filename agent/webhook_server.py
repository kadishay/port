import hashlib
import hmac
import json
import os
import threading
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, Response

app = FastAPI()


def enqueue_issue(issue_number: int) -> None:
    from agent.orchestrator import run_pipeline
    thread = threading.Thread(target=run_pipeline, args=(issue_number,), daemon=True)
    thread.start()


@app.post("/webhook")
async def github_webhook(request: Request) -> Response:
    body = await request.body()
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    sig_header = request.headers.get("X-Hub-Signature-256", "")

    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        return Response(content="Invalid signature", status_code=403)

    event = request.headers.get("X-GitHub-Event", "")
    if event != "issues":
        return Response(content="Ignored", status_code=200)

    payload = json.loads(body)
    if payload.get("action") != "opened":
        return Response(content="Ignored (not opened)", status_code=200)

    issue_number = payload["issue"]["number"]
    print(f"[webhook] Received issue #{issue_number} — dispatching pipeline")
    enqueue_issue(issue_number)
    return Response(content="Accepted", status_code=202)
