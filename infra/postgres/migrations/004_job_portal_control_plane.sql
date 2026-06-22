-- Job portal control plane.
-- Safe for existing environments. Run manually after the Docker services are up:
--   docker exec -i job_miner_postgres psql -U job_miner_app -d job_miner_control < infra/postgres/migrations/004_job_portal_control_plane.sql
--
-- This migration intentionally reuses pipeline_runs, pipeline_run_steps,
-- celery_tasks, celery_task_events, processing_failures, and audit_events.
-- job_portals is the single long-lived portal configuration table.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS job_miner_control;

CREATE TABLE IF NOT EXISTS job_miner_control.job_portals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES job_miner_control.organizations(id),

    -- Stable portal identity. target_id is written to MongoDB and must never be changed
    -- after the first scrape because it participates in deterministic job identity.
    portal_key TEXT NOT NULL,
    target_id TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,

    listing_url TEXT NOT NULL,
    canonical_listing_url TEXT,
    normalized_host TEXT NOT NULL,
    allowed_hosts JSONB NOT NULL DEFAULT '[]'::jsonb,

    source_platform TEXT NOT NULL DEFAULT 'unknown',
    source_platform_confidence NUMERIC(5,4),
    crawl_strategy TEXT,
    profile_name TEXT,
    configuration_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    configuration_version INTEGER NOT NULL DEFAULT 1,

    -- status represents lifecycle/administrative availability. last_run_status is
    -- deliberately separate so one failed refresh does not silently deactivate a portal.
    status TEXT NOT NULL DEFAULT 'draft',
    last_run_status TEXT NOT NULL DEFAULT 'never',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    failure_streak INTEGER NOT NULL DEFAULT 0,

    schedule_expression TEXT,
    next_run_at TIMESTAMPTZ,
    last_run_started_on TIMESTAMPTZ,
    last_successful_run_on TIMESTAMPTZ,
    last_failed_run_on TIMESTAMPTZ,

    max_pages_per_run INTEGER NOT NULL DEFAULT 50,
    max_jobs_per_run INTEGER NOT NULL DEFAULT 500,
    request_rate_limit_per_minute INTEGER NOT NULL DEFAULT 30,
    crawl_timeout_seconds INTEGER NOT NULL DEFAULT 1800,

    -- Lease fields prevent two workers/schedulers from running the same portal.
    lease_token UUID,
    lease_until TIMESTAMPTZ,

    created_by UUID REFERENCES job_miner_control.app_users(id),
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID REFERENCES job_miner_control.app_users(id),
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT uq_job_portals_organization_key UNIQUE (organization_id, portal_key),
    CONSTRAINT chk_job_portals_status CHECK (
        status IN (
            'draft', 'probing', 'ready_for_test', 'test_scraping',
            'ready_for_activation', 'active', 'paused', 'needs_review', 'blocked'
        )
    ),
    CONSTRAINT chk_job_portals_last_run_status CHECK (
        last_run_status IN ('never', 'queued', 'running', 'completed', 'partial', 'failed')
    ),
    CONSTRAINT chk_job_portals_allowed_hosts_array CHECK (jsonb_typeof(allowed_hosts) = 'array'),
    CONSTRAINT chk_job_portals_configuration_object CHECK (jsonb_typeof(configuration_json) = 'object'),
    CONSTRAINT chk_job_portals_max_pages CHECK (max_pages_per_run BETWEEN 1 AND 250),
    CONSTRAINT chk_job_portals_max_jobs CHECK (max_jobs_per_run BETWEEN 1 AND 2000),
    CONSTRAINT chk_job_portals_request_rate CHECK (request_rate_limit_per_minute BETWEEN 1 AND 120),
    CONSTRAINT chk_job_portals_timeout CHECK (crawl_timeout_seconds BETWEEN 30 AND 7200),
    CONSTRAINT chk_job_portals_failure_streak CHECK (failure_streak >= 0)
);

ALTER TABLE job_miner_control.pipeline_runs
    ADD COLUMN IF NOT EXISTS portal_id UUID REFERENCES job_miner_control.job_portals(id);

CREATE INDEX IF NOT EXISTS idx_job_portals_org_status_modified
    ON job_miner_control.job_portals (organization_id, status, modified_on DESC);

CREATE INDEX IF NOT EXISTS idx_job_portals_due_schedule
    ON job_miner_control.job_portals (next_run_at)
    WHERE is_active = TRUE AND status = 'active';

CREATE INDEX IF NOT EXISTS idx_job_portals_lease_until
    ON job_miner_control.job_portals (lease_until)
    WHERE lease_until IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_portal_created
    ON job_miner_control.pipeline_runs (portal_id, created_on DESC)
    WHERE portal_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_events_portal_history
    ON job_miner_control.audit_events (organization_id, entity_type, entity_id, created_on DESC)
    WHERE entity_type = 'job_portal';

DROP TRIGGER IF EXISTS trg_job_portals_set_modified_on ON job_miner_control.job_portals;
CREATE TRIGGER trg_job_portals_set_modified_on
BEFORE UPDATE ON job_miner_control.job_portals
FOR EACH ROW EXECUTE FUNCTION job_miner_control.set_modified_on();

INSERT INTO job_miner_control.app_permissions (permission_key, resource_type, action, description)
VALUES
    ('portal.view', 'job_portal', 'view', 'View job portal configuration and run history'),
    ('portal.manage', 'job_portal', 'manage', 'Create and update job portal configuration'),
    ('portal.run', 'job_portal', 'run', 'Probe, test, activate, and refresh job portals'),
    ('portal.pause', 'job_portal', 'pause', 'Pause or resume job portals')
ON CONFLICT (permission_key) DO NOTHING;

-- Platform admins already receive every permission from the original bootstrap.
-- Keep this explicit and idempotent for databases where roles were created earlier.
INSERT INTO job_miner_control.app_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM job_miner_control.app_roles r
JOIN job_miner_control.app_permissions p ON p.permission_key IN ('portal.view', 'portal.manage', 'portal.run', 'portal.pause')
WHERE r.role_key IN ('platform_admin', 'tenant_admin')
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO job_miner_control.celery_queues (queue_name, description, max_concurrency)
VALUES
    ('portal_probe_queue', 'Safe portal URL probes and profile detection', 2),
    ('portal_scrape_queue', 'Browser and LLM job portal scraping', 2)
ON CONFLICT (queue_name) DO UPDATE
SET description = EXCLUDED.description,
    max_concurrency = EXCLUDED.max_concurrency,
    modified_on = NOW();
