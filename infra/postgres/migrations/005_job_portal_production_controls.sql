-- Production controls for admin-added job portals.
-- Run after 004_job_portal_control_plane.sql.

CREATE SCHEMA IF NOT EXISTS job_miner_control;

ALTER TABLE job_miner_control.job_portals
    ADD COLUMN IF NOT EXISTS scheduler_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS refresh_interval_minutes INTEGER,
    ADD COLUMN IF NOT EXISTS deactivate_after_misses INTEGER NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS min_discovery_coverage_ratio NUMERIC(5,4) NOT NULL DEFAULT 0.2500,
    ADD COLUMN IF NOT EXISTS max_consecutive_failures_before_pause INTEGER NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS detail_retry_attempts INTEGER NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS last_health_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS last_health_checked_on TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_alert_on TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS alert_status TEXT;

ALTER TABLE job_miner_control.job_portals
    DROP CONSTRAINT IF EXISTS chk_job_portals_refresh_interval,
    ADD CONSTRAINT chk_job_portals_refresh_interval CHECK (refresh_interval_minutes IS NULL OR refresh_interval_minutes BETWEEN 15 AND 10080);

ALTER TABLE job_miner_control.job_portals
    DROP CONSTRAINT IF EXISTS chk_job_portals_deactivate_after_misses,
    ADD CONSTRAINT chk_job_portals_deactivate_after_misses CHECK (deactivate_after_misses BETWEEN 1 AND 10);

ALTER TABLE job_miner_control.job_portals
    DROP CONSTRAINT IF EXISTS chk_job_portals_min_coverage,
    ADD CONSTRAINT chk_job_portals_min_coverage CHECK (min_discovery_coverage_ratio BETWEEN 0.0500 AND 1.0000);

ALTER TABLE job_miner_control.job_portals
    DROP CONSTRAINT IF EXISTS chk_job_portals_failure_pause,
    ADD CONSTRAINT chk_job_portals_failure_pause CHECK (max_consecutive_failures_before_pause BETWEEN 1 AND 20);

ALTER TABLE job_miner_control.job_portals
    DROP CONSTRAINT IF EXISTS chk_job_portals_detail_retries,
    ADD CONSTRAINT chk_job_portals_detail_retries CHECK (detail_retry_attempts BETWEEN 0 AND 5);

CREATE INDEX IF NOT EXISTS idx_job_portals_scheduler_due
    ON job_miner_control.job_portals (next_run_at, lease_until)
    WHERE is_active = TRUE AND status = 'active' AND scheduler_enabled = TRUE;

CREATE INDEX IF NOT EXISTS idx_job_portals_health_status
    ON job_miner_control.job_portals (organization_id, last_health_status, modified_on DESC);

CREATE INDEX IF NOT EXISTS idx_file_artifacts_pipeline_category
    ON job_miner_control.file_artifacts (pipeline_run_id, artifact_category, created_on DESC)
    WHERE pipeline_run_id IS NOT NULL;

INSERT INTO job_miner_control.app_permissions (permission_key, resource_type, action, description)
VALUES
    ('portal.override', 'job_portal', 'override', 'Override detected portal profile and crawler configuration'),
    ('portal.health', 'job_portal', 'health', 'View portal health dashboard and operational metrics')
ON CONFLICT (permission_key) DO NOTHING;

INSERT INTO job_miner_control.app_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM job_miner_control.app_roles r
JOIN job_miner_control.app_permissions p ON p.permission_key IN ('portal.override', 'portal.health')
WHERE r.role_key IN ('platform_admin', 'tenant_admin')
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO job_miner_control.celery_queues (queue_name, description, max_concurrency)
VALUES
    ('portal_scheduler_queue', 'Periodic scheduler that enqueues due active portal refreshes', 1),
    ('portal_artifact_queue', 'Portal crawl artifact post-processing and retention work', 1),
    ('recommendation_refresh_queue', 'Controlled recommendation refresh batches after new portal jobs arrive', 2)
ON CONFLICT (queue_name) DO UPDATE
SET description = EXCLUDED.description,
    max_concurrency = EXCLUDED.max_concurrency,
    modified_on = NOW();
