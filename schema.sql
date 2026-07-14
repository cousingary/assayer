-- assayer schema: traces + quarantine tables for unattended LLM pipelines.

CREATE TABLE IF NOT EXISTS assayer_traces (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    pipeline text NOT NULL,
    job_id text,
    item_hash text NOT NULL,
    model text,
    tokens_in integer,
    tokens_out integer,
    cost_usd numeric,
    verdict text NOT NULL CHECK (verdict IN ('pass', 'fail', 'error', 'advisory')),
    checks_json jsonb,
    quarantine_id bigint,
    duration_ms integer,
    -- Sol redteam v4, P0-8: the outbox's own stable id, embedded into a row
    -- at enqueue time so a retried/reclaimed outbox entry can never insert
    -- twice (NULL for rows inserted directly, never via the outbox --
    -- Postgres treats multiple NULLs as distinct under UNIQUE).
    event_id text UNIQUE,
    -- Sol redteam v4, P1-13: "standard" (nothing sensitive detected/redacted)
    -- or "sensitive" (redaction found and masked something in this row's
    -- payload/reasons).
    data_classification text NOT NULL DEFAULT 'standard',
    payload_encrypted boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_assayer_traces_pipeline_ts
    ON assayer_traces (pipeline, ts);

CREATE INDEX IF NOT EXISTS idx_assayer_traces_pipeline_item_hash
    ON assayer_traces (pipeline, item_hash);

CREATE TABLE IF NOT EXISTS assayer_quarantine (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    pipeline text NOT NULL,
    item_hash text NOT NULL,
    payload_json jsonb,
    input_ref text,
    reasons_json jsonb,
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'released', 'discarded')),
    event_id text UNIQUE,
    data_classification text NOT NULL DEFAULT 'standard',
    payload_encrypted boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS idx_assayer_quarantine_pipeline_status
    ON assayer_quarantine (pipeline, status);

-- Self-hosted Supabase: PostgREST serves the REST API under the service_role /
-- anon roles and caches the schema; without these the new tables 404 via supabase-py.
GRANT ALL ON TABLE assayer_traces, assayer_quarantine TO service_role;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO service_role;
NOTIFY pgrst, 'reload schema';
