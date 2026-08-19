# Monitoring & Observability Design

**Date:** 2026-08-19
**Status:** Design only — not required to be fully operational for the assignment deliverable.

## Purpose

Give the bug-triage agent a lightweight, real observability layer that demonstrates
the pipeline can be measured and reasoned about over time — cost, latency, agent
confidence, and downstream human outcomes per issue. This is primarily a demo/write-up
artifact: it should genuinely record the two demo bug runs, but doesn't need to survive
production load or handle scale.

## Storage: Supabase (Postgres)

Free tier, real Postgres, and its built-in table editor doubles as a zero-code viewer.
No ORM, no SDK dependency — writes go through a plain HTTP POST to Supabase's
PostgREST endpoint (`/rest/v1/bug_runs`) using the service-role key.

## Schema

One table, `bug_runs`, one row per pipeline run (one run = one issue processed once).

| Column | Type | Source | Notes |
|---|---|---|---|
| `id` | serial PK | auto | — |
| `issue_number` | int | `ctx.issue_number` | natural key |
| `issue_title` | text | `ctx.issue_title` | dashboard readability |
| `bug_type` | text (`BE`/`FE`) | derived from `ctx.affected_files` (`frontend/` prefix → FE, else BE) | user item 0 |
| `created_at` | timestamptz | `time.time()` at pipeline start | — |
| `duration_seconds` | float | wall-clock in `orchestrator.py` | user item 1 |
| `cost_usd` | float | `tracker.total_cost()` | user item 2 |
| `severity` | text | `ctx.severity` | user item 6 — triage agent's estimate |
| `risk_level` | text | `ctx.risk_level` | user item 7 — solve agent's estimate |
| `risk_reasons` | jsonb | `ctx.risk_reasons` | supporting detail for risk_level |
| `autonomy_decision` | text (`AUTO_PR`/`HITL_REQUIRED`) | `ctx.autonomy_decision` | causal link between risk and what happened next |
| `opus_fallback_used` | bool | true if either low-confidence retry path fired (triage 2d or solve 4b²) | signals when Haiku hedged — cost/confidence story |
| `pipeline_outcome` | text enum | `fixed_auto_pr` / `hitl_pending` / `hitl_rejected` / `not_a_bug` / `unable_to_reproduce` / `crashed` | unifies all terminal branches into one column |
| `pr_url` | text, nullable | `ctx.pr_url` | traceability |
| `human_merged_as_is` | bool, nullable, **mock** | simulated | user item 3 |
| `human_rejected` | bool, nullable, **mock** | simulated | user item 4 |
| `human_added_comment` | bool, nullable, **mock** | simulated | user item 5 |

All three human-outcome fields are simulated — the pipeline has no real signal for
post-PR GitHub review activity, and the existing HITL reject path (in `solve.py`) is a
pipeline-internal approval/reject gate, not a proxy for what a human does after a PR is
opened, so it isn't treated as real data here either.

## Write path

Single insert point: `orchestrator.py`'s existing `finally` block. It already has `ctx`
fully populated and `tracker.summary()` computed by the time it runs, and it's the one
place that sees every terminal branch (not_a_bug, unable_to_reproduce, normal
completion, crash) regardless of which path the run took.

New module `agent/telemetry.py`:

```python
def record_run(ctx: BugContext, tracker: CostTracker, duration_seconds: float, bug_type: str) -> None:
    ...  # builds the row dict, calls _mock_human_outcomes(), POSTs to Supabase REST
```

Wrapped in try/except so a telemetry failure never breaks the pipeline — mirrors the
existing pattern where `_notify_thread` failures don't abort a run.

`orchestrator.py` changes: capture `start = time.time()` at the top of `run_pipeline`,
compute `bug_type` from `ctx.affected_files`, call `record_run(...)` in `finally`
alongside the existing `cost_summary` print/notify. ~5 lines added, no changes to
`triage.py` or `solve.py`.

## Mock strategy

`_mock_human_outcomes(ctx)` generates the three human-outcome fields with weighting
driven by `risk_level` and `autonomy_decision`, so the two real demo-bug rows produce a
plausible trend rather than a coin flip:

- LOW/MEDIUM risk + AUTO_PR → high probability `human_merged_as_is=True`, low
  probability of a comment or rejection.
- HIGH/ESCALATE risk or HITL_REQUIRED → lower merge-as-is probability, higher chance of
  a comment and/or rejection.

This logic lives entirely in `telemetry.py`, clearly separated from the real fields it
sits next to in the same row.

## Dashboard

Single static file, `dashboard.html`, doing a client-side fetch against Supabase's REST
API with a read-only anon key — no server, no framework. Shows:

- A table of runs (issue, bug_type, severity, risk, outcome, cost, duration).
- Aggregate stats: avg cost/time by `bug_type`, severity distribution, merge rate by
  `risk_level`.

Simple enough to open in a browser for the video walkthrough. Supabase's own table
editor remains available as a fallback/raw-data view without any extra code.

## Implementation footprint

- 1 new Postgres table (`bug_runs`), created via Supabase SQL editor.
- 1 new file: `agent/telemetry.py`.
- 1 new file: `dashboard.html`.
- ~5 lines added to `agent/orchestrator.py` (start-time capture, bug_type derivation,
  `record_run` call in `finally`).
- No changes to `agent/triage.py` or `agent/solve.py`.

## Out of scope (YAGNI)

- No auth beyond Supabase's built-in row-level security / anon-key read access.
- No historical backfill/seeding script — dashboard will only show real rows from
  actual runs, which is acceptable for a demo/write-up artifact per current scope.
- No alerting, no retries/queueing on write failure, no schema migrations tooling.
- No integration with Phase 2 Slack notifications — telemetry write is independent of
  the existing `_notify`/`_notify_thread` status-update path.
