# Slack App Setup — Bug Triage Agent

The agent uses Slack for two things:
1. **Status updates** — every pipeline step posts to `#bug-triage` as a thread
2. **HITL** — humans approve/reject fixes by replying `/approve` or `/reject` in the thread

This replaces GitHub comment polling when `SLACK_APP_TOKEN` is set.

---

## Step 1 — Create the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: `Bug Triage Agent`
3. Pick your workspace → **Create App**

---

## Step 2 — Enable Socket Mode

Socket Mode lets the agent receive events without a public URL (no ngrok needed for Slack).

1. **Settings → Socket Mode → Enable Socket Mode**
2. Generate an App-Level Token:
   - Token name: `socket-token`
   - Scope: `connections:write`
   - Click **Generate**
3. Copy the token — this is your `SLACK_APP_TOKEN` (starts with `xapp-`)

---

## Step 3 — Subscribe to Events

1. **Event Subscriptions → Enable Events**
2. Under **Subscribe to bot events**, add:
   - `message.channels` (messages in public channels)
   - `message.groups` (messages in private channels)
3. **Save Changes**

---

## Step 4 — Add Bot Token Scopes

1. **OAuth & Permissions → Bot Token Scopes** → Add:
   - `chat:write` — post messages
   - `channels:read` — list channels

> **Note:** `channels:history` and `groups:history` are not in the default scope picker. They get added automatically when you enable the `message.channels` / `message.groups` event subscriptions in Step 3. If they don't appear automatically, search for them by name in the scope picker and add them manually.

---

## Step 5 — Install to Workspace

1. **OAuth & Permissions → Install to Workspace**
2. Click **Allow**
3. Copy the **Bot User OAuth Token** — this is your `SLACK_BOT_TOKEN` (starts with `xoxb-`)

---

## Step 6 — Invite Bot to Channel

In Slack, open your `#bug-triage` channel (or create it) and run:

```
/invite @Bug Triage Agent
```

---

## Step 7 — Update .env

```bash
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
SLACK_CHANNEL=#bug-triage
```

---

## How It Works

Once configured, the agent automatically switches to Slack mode:

| Without Slack | With Slack |
|--------------|------------|
| Pipeline logs print to stdout | Every step posts to `#bug-triage` |
| HITL approval via GitHub comments (`/approve`) | HITL approval via Slack thread reply (`/approve`) |
| No notifications | Thread per issue; all updates in that thread |

The switch is automatic — if `SLACK_BOT_TOKEN` is present, Slack is used. If absent, stdout + GitHub comments are used. No code changes needed.

---

## Testing

Start the agent and open an issue. You should see:

```
📥 Issue #42 received: "Bug: tasks due in next 38h flagged as overdue" — starting triage
🔬 Triage complete — Severity: HIGH | Confidence: 92%
   cc: @dev-who-introduced-it (introduced), @top-contributor (expert)
🔧 Starting automated fix for #42...
✅ PR opened: https://github.com/kadishay/vikunja/pull/8
```

For HITL bugs, the agent posts the diff in the thread and waits for your reply.
