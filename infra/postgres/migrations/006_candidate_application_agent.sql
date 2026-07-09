-- Candidate application agent control-plane tables.
-- Run locally after docker compose is up:
--   docker exec -i job_miner_postgres psql -U job_miner_app -d job_miner_control < infra/postgres/migrations/006_candidate_application_agent.sql

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_application_batches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    batch_status TEXT NOT NULL DEFAULT 'queued',
    requested_job_count INTEGER NOT NULL DEFAULT 0,
    queued_job_count INTEGER NOT NULL DEFAULT 0,
    running_job_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    needs_review_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    celery_task_uuid TEXT,
    langgraph_thread_id TEXT,
    email_status TEXT NOT NULL DEFAULT 'placeholder',
    started_on TIMESTAMPTZ,
    completed_on TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_application_batches_candidate
    ON job_miner_control.candidate_application_batches (app_user_id, candidate_id, created_on DESC);

CREATE INDEX IF NOT EXISTS idx_application_batches_status
    ON job_miner_control.candidate_application_batches (batch_status, created_on DESC);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_application_job_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    batch_id UUID NOT NULL REFERENCES job_miner_control.candidate_application_batches(id) ON DELETE CASCADE,
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    application_id UUID REFERENCES job_miner_control.candidate_job_applications(id),
    run_status TEXT NOT NULL DEFAULT 'queued',
    apply_strategy TEXT,
    portal_domain TEXT,
    apply_url TEXT,
    langgraph_thread_id TEXT,
    celery_task_uuid TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    error_type TEXT,
    error_message TEXT,
    external_confirmation_id TEXT,
    submitted_on TIMESTAMPTZ,
    started_on TIMESTAMPTZ,
    completed_on TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_application_job_run_once UNIQUE (batch_id, app_user_id, candidate_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_application_job_runs_batch
    ON job_miner_control.candidate_application_job_runs (batch_id, run_status);

CREATE INDEX IF NOT EXISTS idx_application_job_runs_candidate
    ON job_miner_control.candidate_application_job_runs (app_user_id, candidate_id, created_on DESC);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    notification_type TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'candidate_portal',
    status TEXT NOT NULL DEFAULT 'visible',
    title TEXT NOT NULL,
    body TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    sent_on TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_candidate_notifications_candidate
    ON job_miner_control.candidate_notifications (app_user_id, candidate_id, created_on DESC);

CREATE TABLE IF NOT EXISTS job_miner_control.application_portal_strategies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    portal_domain TEXT NOT NULL,
    strategy_key TEXT NOT NULL,
    strategy_type TEXT NOT NULL DEFAULT 'manual_review',
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    requires_login BOOLEAN NOT NULL DEFAULT FALSE,
    supports_auto_submit BOOLEAN NOT NULL DEFAULT FALSE,
    supports_resume_upload BOOLEAN NOT NULL DEFAULT FALSE,
    max_concurrent_applications INTEGER NOT NULL DEFAULT 1,
    rate_limit_per_minute INTEGER NOT NULL DEFAULT 10,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_application_portal_strategy UNIQUE (portal_domain, strategy_key)
);

CREATE INDEX IF NOT EXISTS idx_application_portal_strategies_domain
    ON job_miner_control.application_portal_strategies (portal_domain, is_enabled);
