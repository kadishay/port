# Monitoring & Observability

Per-issue telemetry (cost, duration, severity/risk estimates, autonomy outcome,
simulated human-review outcomes) recorded to Supabase on every pipeline run,
plus a static dashboard to view it.

**Design doc:** `docs/superpowers/specs/2026-08-19-monitoring-observability-design.md`
**Implementation plan:** `docs/superpowers/plans/2026-08-19-monitoring-observability.md`

## Architecture

```
run_pipeline()
  └─ finally:
       record_run(ctx, tracker, duration, crashed)   ← agent/telemetry.py
         └─ POST https://<project>.supabase.co/rest/v1/bug_runs
              (service_role key — bypasses RLS)

dashboard.html (static, client-side)
  └─ fetch GET .../rest/v1/bug_runs
       (anon key — read-only via RLS policy)
```

`record_run` is called **first** in `orchestrator.py`'s `finally` block (ahead
of the Slack cost-summary notification), and is wrapped in a single
`try/except Exception` around both `build_row()` and the HTTP call — it is
guaranteed to never raise, so a broken telemetry write can never break the
bug-fixing pipeline itself.

## Schema (`agent/telemetry_schema.sql`)

| Column | Notes |
|---|---|
| `issue_number`, `issue_title` | — |
| `bug_type` | `BE` / `FE` / `UNKNOWN` — derived from `ctx.affected_files` (`frontend/` prefix → FE) |
| `created_at` | DB-assigned (`default now()`) — insert time, not pipeline-start time (see Known Limitations) |
| `duration_seconds`, `cost_usd` | — |
| `severity`, `risk_level`, `risk_reasons` | Triage's severity estimate, solve's risk estimate |
| `autonomy_decision` | `AUTO_PR` / `HITL_REQUIRED` |
| `opus_fallback_used` | true if the Opus low-confidence retry fired anywhere in the run |
| `pipeline_outcome` | `fixed_auto_pr` / `hitl_no_pr` / `not_a_bug` / `unable_to_reproduce` / `crashed` / `no_pr_opened` — see note below |
| `pr_url` | null if no PR was opened |
| `human_merged_as_is`, `human_rejected`, `human_added_comment` | **All three are simulated** (random, weighted by `risk_level`/`autonomy_decision`) — the pipeline has no real signal for post-PR GitHub review activity. **Null when `pr_url` is null** — there's nothing for a human to have merged/rejected/commented on. |

**`pipeline_outcome` naming note:** `hitl_no_pr` and `no_pr_opened` are
deliberately non-committal labels. `solve.py` (off-limits to modify per this
plan's constraints) doesn't expose *why* a `HITL_REQUIRED` run ended with no
PR — it could be an actual human rejection, a 30-minute approval timeout, or
the fix agent producing no file changes to commit. All three collapse into
`hitl_no_pr`. Similarly `no_pr_opened` covers an `AUTO_PR` decision where PR
creation itself never happened.

## Setup

1. Create a free Supabase project.
2. In its **SQL Editor**, run `agent/telemetry_schema.sql` (creates the
   `bug_runs` table + a `select`-only RLS policy for the `anon` role).
3. From **Project Settings → API**, copy:
   - **Project URL** → `.env`'s `SUPABASE_URL`
   - **`service_role` key** → `.env`'s `SUPABASE_SERVICE_KEY` (bypasses RLS —
     server-side only, never commit this one)
   - **`anon` `public` key** → `dashboard.html`'s `SUPABASE_ANON_KEY` constant
     (read-only via RLS — this one is meant to be committed/public)
4. Open `dashboard.html` directly in a browser — no server, no build step.

If `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` are unset, `record_run` prints a
`[telemetry] ... skipping telemetry write` line and no-ops — the pipeline
runs normally without recording anything.

## Gotchas discovered running this for real

- **`python` vs `python3`.** This machine's shell has no `python` on `PATH`,
  only `python3`. `docs/setup.md`'s examples say `python -m agent.main ...`
  — if that silently does nothing (no output at all, not even an error from
  the agent), it's the shell failing on `command not found: python` before
  the agent ever starts. Use `python3`.
- **The webhook server doesn't hot-reload.** `agent/main.py --serve` runs
  `uvicorn` with `reload=False`. If the server process was started before a
  code change (e.g. before telemetry was wired in), it keeps running the
  *old* in-memory code until restarted — no error, just silently missing
  behavior. If telemetry isn't showing up for a webhook-triggered run, check
  whether the server process predates the code that's supposed to write it.
- **Already-has-a-PR issues are silently skipped, by design.** If
  `get_open_pr_for_branch(fix_branch)` finds an existing open PR for an
  issue, `run_pipeline` returns before `CostTracker`/`record_run` are even
  created — this is intentional (avoids duplicate telemetry rows and
  duplicate fix attempts for issues that already have a PR out), but it means
  re-running `--issue N` against an issue you've already triaged won't
  produce a new row. Use a fresh, untriaged issue (no `severity:*` label, no
  open PR on `fix/issue-N`) to generate new telemetry.
- **`PLAYWRIGHT_HEADLESS`** defaults to headless (`true`) unless explicitly
  set to `"false"` in `.env`. A visible Chrome window popping up during a
  frontend-bug run is Playwright (`agent/tools/browser_tools.py`) — not
  Claude-in-Chrome.

## Known limitations

- **Mocked fields are mocked.** `human_merged_as_is`, `human_rejected`, and
  `human_added_comment` are simulated at write time — never treat them as
  real reviewer data. They exist to make the dashboard's charts look
  populated for a demo/write-up, weighted plausibly by risk level.
- **Severity/risk/autonomy defaults leak through for early-exit runs.**
  `BugContext`'s defaults (`severity=MEDIUM`, `risk_level=MEDIUM`,
  `autonomy_decision=HITL_REQUIRED`) get recorded as-is for `crashed` /
  `not_a_bug` / `unable_to_reproduce` runs that never reached a real
  triage/solve verdict — there's no field on `BugContext` distinguishing
  "never actually triaged" from "triaged and it happened to be MEDIUM."
  Fixing this cleanly would mean adding an explicit "triage completed" flag
  to `BugContext`, which is shared with `triage.py`/`solve.py` — deliberately
  left alone for this iteration. When reading the dashboard's severity or
  autonomy charts, treat rows with `pipeline_outcome` in
  (`crashed`, `not_a_bug`, `unable_to_reproduce`) as noise on those two axes.
- **Dashboard is light-mode only, single static file, no error retry.** By
  design — it's meant to be a demo/write-up artifact, not a production
  surface.
