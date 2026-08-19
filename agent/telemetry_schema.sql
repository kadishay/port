create table if not exists bug_runs (
    id bigint generated always as identity primary key,
    issue_number integer not null,
    issue_title text not null,
    bug_type text not null,
    created_at timestamptz not null default now(),
    duration_seconds double precision not null,
    cost_usd double precision not null,
    severity text not null,
    risk_level text not null,
    risk_reasons jsonb not null default '[]'::jsonb,
    autonomy_decision text not null,
    opus_fallback_used boolean not null default false,
    pipeline_outcome text not null,
    pr_url text,
    human_merged_as_is boolean,
    human_rejected boolean,
    human_added_comment boolean
);

alter table bug_runs enable row level security;

-- Dashboard reads with the anon key; inserts use the service_role key, which
-- bypasses RLS, so no insert policy is needed here.
create policy "public read access" on bug_runs
    for select
    to anon
    using (true);
