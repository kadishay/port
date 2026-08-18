import os
import time
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


class SlackClient:
    def __init__(self):
        self._web = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        self._channel = os.environ.get("SLACK_CHANNEL", "#bug-triage")
        self._approvals: dict[str, bool | None] = {}

    def post_status(self, message: str) -> str:
        result = self._web.chat_postMessage(channel=self._channel, text=message)
        return result["ts"]

    def post_to_thread(self, thread_ts: str, message: str) -> None:
        self._web.chat_postMessage(channel=self._channel, thread_ts=thread_ts, text=message)

    def wait_for_approval(self, thread_ts: str, timeout: int = 1800) -> bool:
        app_token = os.environ.get("SLACK_APP_TOKEN", "")
        if not app_token:
            raise RuntimeError("SLACK_APP_TOKEN required for Slack HITL")

        self._approvals[thread_ts] = None
        socket = SocketModeClient(app_token=app_token, web_client=self._web)

        def handle(client: SocketModeClient, req: SocketModeRequest):
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            if req.type != "events_api":
                return
            event = req.payload.get("event", {})
            if event.get("type") != "message" or event.get("thread_ts") != thread_ts:
                return
            text = event.get("text", "").strip().lower()
            if text.startswith("/approve"):
                self._approvals[thread_ts] = True
            elif text.startswith("/reject"):
                self._approvals[thread_ts] = False

        socket.socket_mode_request_listeners.append(handle)
        socket.connect()

        deadline = time.time() + timeout
        while time.time() < deadline:
            decision = self._approvals.get(thread_ts)
            if decision is not None:
                socket.close()
                return decision
            time.sleep(5)

        socket.close()
        from agent.hitl import HITLTimeout
        raise HITLTimeout(f"No Slack response in {timeout}s")
