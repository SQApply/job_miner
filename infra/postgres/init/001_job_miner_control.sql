CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS job_miner_control;

CREATE OR REPLACE FUNCTION job_miner_control.set_modified_on()
RETURNS TRIGGER AS $$
BEGIN
    NEW.modified_on = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS job_miner_control.organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_code TEXT NOT NULL UNIQUE,
    organization_name TEXT NOT NULL,
    keycloak_realm TEXT NOT NULL DEFAULT 'job-miner',
    keycloak_group_path TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.keycloak_realm_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    realm_name TEXT NOT NULL UNIQUE,
    issuer_url TEXT NOT NULL,
    jwks_url TEXT NOT NULL,
    authorization_url TEXT,
    token_url TEXT,
    userinfo_url TEXT,
    admin_api_base_url TEXT,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active',
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.keycloak_client_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    realm_config_id UUID REFERENCES job_miner_control.keycloak_realm_configs(id),
    client_id TEXT NOT NULL,
    client_name TEXT,
    client_type TEXT NOT NULL DEFAULT 'oidc',
    expected_audience TEXT,
    allowed_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_service_client BOOLEAN NOT NULL DEFAULT FALSE,
    status TEXT NOT NULL DEFAULT 'active',
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_keycloak_client UNIQUE (realm_config_id, client_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    keycloak_realm TEXT NOT NULL,
    keycloak_user_id TEXT NOT NULL,
    username TEXT,
    email TEXT,
    normalized_email TEXT GENERATED ALWAYS AS (lower(NULLIF(btrim(email), ''))) STORED,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    full_name TEXT,
    first_name TEXT,
    last_name TEXT,
    preferred_username TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_login_on TIMESTAMPTZ,
    last_token_seen_on TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_app_user_keycloak UNIQUE (keycloak_realm, keycloak_user_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_user_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    keycloak_realm TEXT NOT NULL,
    identity_provider TEXT NOT NULL DEFAULT 'keycloak',
    email TEXT,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_app_user_identity UNIQUE (issuer, subject)
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    role_key TEXT NOT NULL UNIQUE,
    role_name TEXT NOT NULL,
    description TEXT,
    is_system_role BOOLEAN NOT NULL DEFAULT FALSE,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    permission_key TEXT NOT NULL UNIQUE,
    resource_type TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_role_permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    role_id UUID NOT NULL REFERENCES job_miner_control.app_roles(id),
    permission_id UUID NOT NULL REFERENCES job_miner_control.app_permissions(id),
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_role_permission UNIQUE (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.keycloak_role_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    keycloak_realm TEXT NOT NULL,
    keycloak_client_id TEXT,
    keycloak_role_name TEXT,
    keycloak_group_path TEXT,
    mapping_type TEXT NOT NULL,
    app_role_id UUID NOT NULL REFERENCES job_miner_control.app_roles(id),
    priority INTEGER NOT NULL DEFAULT 100,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.app_user_role_assignments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    app_role_id UUID NOT NULL REFERENCES job_miner_control.app_roles(id),
    assignment_source TEXT NOT NULL DEFAULT 'keycloak_role',
    keycloak_role_name TEXT,
    keycloak_group_path TEXT,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_until TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_user_role_assignment UNIQUE (app_user_id, app_role_id, assignment_source, keycloak_role_name, keycloak_group_path)
);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_user_links (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    resume_id TEXT,
    mongo_database_name TEXT NOT NULL DEFAULT 'job_miner',
    mongo_collection_name TEXT NOT NULL DEFAULT 'candidate_tower_records',
    is_primary BOOLEAN NOT NULL DEFAULT TRUE,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_candidate_link_user_candidate UNIQUE (app_user_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.api_request_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID REFERENCES job_miner_control.app_users(id),
    request_id TEXT NOT NULL UNIQUE,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER,
    duration_ms INTEGER,
    keycloak_realm TEXT,
    keycloak_user_id TEXT,
    keycloak_client_id TEXT,
    roles_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
    error_message TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.audit_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    actor_app_user_id UUID REFERENCES job_miner_control.app_users(id),
    actor_keycloak_user_id TEXT,
    actor_keycloak_client_id TEXT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    before_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.pipeline_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    run_session_id TEXT NOT NULL UNIQUE,
    pipeline_name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    trigger_type TEXT NOT NULL DEFAULT 'api',
    triggered_by_app_user_id UUID REFERENCES job_miner_control.app_users(id),
    triggered_by_keycloak_user_id TEXT,
    started_on TIMESTAMPTZ,
    completed_on TIMESTAMPTZ,
    duration_seconds NUMERIC(12, 3),
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.pipeline_run_steps (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id UUID NOT NULL REFERENCES job_miner_control.pipeline_runs(id),
    step_key TEXT NOT NULL,
    step_name TEXT NOT NULL,
    step_order INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    started_on TIMESTAMPTZ,
    completed_on TIMESTAMPTZ,
    duration_seconds NUMERIC(12, 3),
    input_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_pipeline_step UNIQUE (pipeline_run_id, step_key)
);

CREATE TABLE IF NOT EXISTS job_miner_control.pipeline_run_summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pipeline_run_id UUID REFERENCES job_miner_control.pipeline_runs(id),
    pipeline_run_step_id UUID REFERENCES job_miner_control.pipeline_run_steps(id),
    summary_type TEXT NOT NULL,
    summary_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.celery_queues (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_name TEXT NOT NULL UNIQUE,
    description TEXT,
    max_concurrency INTEGER NOT NULL DEFAULT 1,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.celery_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    task_uuid TEXT NOT NULL UNIQUE,
    task_name TEXT NOT NULL,
    queue_name TEXT NOT NULL DEFAULT 'default',
    pipeline_run_id UUID REFERENCES job_miner_control.pipeline_runs(id),
    pipeline_run_step_id UUID REFERENCES job_miner_control.pipeline_run_steps(id),
    requested_by_app_user_id UUID REFERENCES job_miner_control.app_users(id),
    status TEXT NOT NULL DEFAULT 'queued',
    priority INTEGER NOT NULL DEFAULT 5,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_type TEXT,
    error_message TEXT,
    traceback TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    started_on TIMESTAMPTZ,
    completed_on TIMESTAMPTZ,
    worker_name TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.celery_task_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    celery_task_id UUID REFERENCES job_miner_control.celery_tasks(id),
    event_type TEXT NOT NULL,
    message TEXT,
    progress_percent NUMERIC(5, 2),
    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.processing_failures (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    pipeline_run_id UUID REFERENCES job_miner_control.pipeline_runs(id),
    pipeline_run_step_id UUID REFERENCES job_miner_control.pipeline_run_steps(id),
    celery_task_id UUID REFERENCES job_miner_control.celery_tasks(id),
    failure_id TEXT NOT NULL UNIQUE,
    entity_type TEXT,
    entity_id TEXT,
    error_type TEXT,
    error_message TEXT,
    traceback TEXT,
    failed_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    resolved_on TIMESTAMPTZ,
    resolution_notes TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.file_artifacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    pipeline_run_id UUID REFERENCES job_miner_control.pipeline_runs(id),
    pipeline_run_step_id UUID REFERENCES job_miner_control.pipeline_run_steps(id),
    artifact_id TEXT NOT NULL UNIQUE,
    artifact_category TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    original_file_name TEXT,
    original_relative_path TEXT,
    file_extension TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    sha256 TEXT,
    storage_mode TEXT NOT NULL DEFAULT 'temporary_local',
    external_uri TEXT,
    mongo_database_name TEXT,
    mongo_collection_name TEXT,
    mongo_record_key TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_job_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    preferred_locations JSONB NOT NULL DEFAULT '[]'::jsonb,
    remote_preference TEXT,
    preferred_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_roles JSONB NOT NULL DEFAULT '[]'::jsonb,
    preferred_employment_types JSONB NOT NULL DEFAULT '[]'::jsonb,
    salary_expectation TEXT,
    notice_period TEXT,
    work_authorization TEXT,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_candidate_preferences UNIQUE (app_user_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_saved_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    match_run_id TEXT,
    source_collection TEXT,
    status TEXT NOT NULL DEFAULT 'saved',
    saved_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_candidate_saved_job UNIQUE (app_user_id, candidate_id, job_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_job_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    match_run_id TEXT,
    application_status TEXT NOT NULL DEFAULT 'selected',
    apply_url TEXT,
    applied_on TIMESTAMPTZ,
    last_status_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    agent_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    agent_consent_status TEXT NOT NULL DEFAULT 'not_requested',
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_candidate_application UNIQUE (app_user_id, candidate_id, job_id)
);

CREATE TABLE IF NOT EXISTS job_miner_control.application_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    application_id UUID REFERENCES job_miner_control.candidate_job_applications(id),
    app_user_id UUID REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT,
    job_id TEXT,
    event_type TEXT NOT NULL,
    event_source TEXT NOT NULL DEFAULT 'candidate_portal',
    message TEXT,
    event_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);


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

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_recommendation_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    match_run_id TEXT,
    job_id TEXT NOT NULL,
    recommendation_source TEXT NOT NULL,
    viewed_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    clicked_on TIMESTAMPTZ,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_app_users_email ON job_miner_control.app_users (email);
CREATE INDEX IF NOT EXISTS idx_app_users_normalized_email ON job_miner_control.app_users (organization_id, normalized_email);
CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_org_normalized_email ON job_miner_control.app_users (organization_id, normalized_email) WHERE normalized_email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_app_user_identities_user ON job_miner_control.app_user_identities (app_user_id);
CREATE INDEX IF NOT EXISTS idx_app_user_identities_email ON job_miner_control.app_user_identities (organization_id, lower(trim(email))) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_candidate_links_user ON job_miner_control.candidate_user_links (app_user_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_primary_link_per_user ON job_miner_control.candidate_user_links (app_user_id) WHERE is_primary = TRUE AND is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created_on ON job_miner_control.pipeline_runs (created_on DESC);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON job_miner_control.pipeline_runs (status);
CREATE INDEX IF NOT EXISTS idx_celery_tasks_status ON job_miner_control.celery_tasks (status);
CREATE INDEX IF NOT EXISTS idx_processing_failures_created_on ON job_miner_control.processing_failures (created_on DESC);
CREATE INDEX IF NOT EXISTS idx_saved_jobs_candidate ON job_miner_control.candidate_saved_jobs (candidate_id, status);
CREATE INDEX IF NOT EXISTS idx_applications_candidate ON job_miner_control.candidate_job_applications (candidate_id, application_status);
CREATE INDEX IF NOT EXISTS idx_application_batches_candidate ON job_miner_control.candidate_application_batches (app_user_id, candidate_id, created_on DESC);
CREATE INDEX IF NOT EXISTS idx_application_batches_status ON job_miner_control.candidate_application_batches (batch_status, created_on DESC);
CREATE INDEX IF NOT EXISTS idx_application_job_runs_batch ON job_miner_control.candidate_application_job_runs (batch_id, run_status);
CREATE INDEX IF NOT EXISTS idx_application_job_runs_candidate ON job_miner_control.candidate_application_job_runs (app_user_id, candidate_id, created_on DESC);
CREATE INDEX IF NOT EXISTS idx_candidate_notifications_candidate ON job_miner_control.candidate_notifications (app_user_id, candidate_id, created_on DESC);
CREATE INDEX IF NOT EXISTS idx_application_portal_strategies_domain ON job_miner_control.application_portal_strategies (portal_domain, is_enabled);
CREATE INDEX IF NOT EXISTS idx_api_logs_created_on ON job_miner_control.api_request_logs (created_on DESC);

INSERT INTO job_miner_control.organizations (organization_code, organization_name, keycloak_realm, keycloak_group_path)
VALUES ('default', 'Default Tenant', 'job-miner', '/tenants/default')
ON CONFLICT (organization_code) DO NOTHING;

INSERT INTO job_miner_control.app_roles (role_key, role_name, is_system_role)
VALUES
('platform_admin', 'Platform Admin', TRUE),
('tenant_admin', 'Tenant Admin', TRUE),
('candidate', 'Candidate', TRUE),
('recruiter', 'Recruiter', TRUE),
('viewer', 'Viewer', TRUE)
ON CONFLICT (role_key) DO NOTHING;

INSERT INTO job_miner_control.app_permissions (permission_key, resource_type, action, description)
VALUES
('admin.view', 'admin', 'view', 'View admin dashboard'),
('pipeline.run', 'pipeline', 'run', 'Run pipeline tasks'),
('pipeline.view', 'pipeline', 'view', 'View pipeline runs'),
('candidate.view_self', 'candidate', 'view_self', 'View own candidate profile'),
('recommendations.view_self', 'recommendations', 'view_self', 'View own recommendations'),
('applications.manage_self', 'applications', 'manage_self', 'Save/select/apply own jobs'),
('feedback.create_self', 'feedback', 'create_self', 'Create feedback for own recommendations')
ON CONFLICT (permission_key) DO NOTHING;

INSERT INTO job_miner_control.keycloak_realm_configs (realm_name, issuer_url, jwks_url, authorization_url, token_url, userinfo_url, is_default)
VALUES ('job-miner', 'http://localhost:8080/realms/job-miner', 'http://localhost:8080/realms/job-miner/protocol/openid-connect/certs', 'http://localhost:8080/realms/job-miner/protocol/openid-connect/auth', 'http://localhost:8080/realms/job-miner/protocol/openid-connect/token', 'http://localhost:8080/realms/job-miner/protocol/openid-connect/userinfo', TRUE)
ON CONFLICT (realm_name) DO NOTHING;

INSERT INTO job_miner_control.celery_queues (queue_name, description, max_concurrency)
VALUES
('etl_queue', 'Warehouse and ETL tasks', 2),
('embedding_queue', 'Embedding generation and Qdrant indexing', 1),
('matching_queue', 'Baseline matching and comparison', 1),
('llm_queue', 'LLM reranking tasks', 1),
('maintenance_queue', 'Reports, cleanup, and utilities', 1)
ON CONFLICT (queue_name) DO NOTHING;

INSERT INTO job_miner_control.app_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM job_miner_control.app_roles r
CROSS JOIN job_miner_control.app_permissions p
WHERE r.role_key = 'platform_admin'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO job_miner_control.app_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM job_miner_control.app_roles r
JOIN job_miner_control.app_permissions p ON p.permission_key IN ('admin.view', 'pipeline.run', 'pipeline.view')
WHERE r.role_key = 'tenant_admin'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO job_miner_control.app_role_permissions (role_id, permission_id)
SELECT r.id, p.id
FROM job_miner_control.app_roles r
JOIN job_miner_control.app_permissions p ON p.permission_key IN ('candidate.view_self', 'recommendations.view_self', 'applications.manage_self', 'feedback.create_self')
WHERE r.role_key = 'candidate'
ON CONFLICT (role_id, permission_id) DO NOTHING;

INSERT INTO job_miner_control.keycloak_role_mappings (organization_id, keycloak_realm, keycloak_client_id, keycloak_role_name, mapping_type, app_role_id)
SELECT o.id, 'job-miner', 'job-miner-api', r.role_key, 'client_role_to_app_role', r.id
FROM job_miner_control.organizations o
CROSS JOIN job_miner_control.app_roles r
WHERE o.organization_code = 'default'
ON CONFLICT DO NOTHING;

INSERT INTO job_miner_control.keycloak_role_mappings (organization_id, keycloak_realm, keycloak_group_path, mapping_type, app_role_id, priority)
SELECT o.id, 'job-miner', group_path, 'group_to_app_role', r.id, 50
FROM job_miner_control.organizations o
JOIN (VALUES
    ('/tenants/default/admins', 'tenant_admin'),
    ('/tenants/default/candidates', 'candidate'),
    ('/tenants/default/recruiters', 'recruiter')
) AS gm(group_path, role_key) ON TRUE
JOIN job_miner_control.app_roles r ON r.role_key = gm.role_key
WHERE o.organization_code = 'default'
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION job_miner_control.touch_modified_on_for_all()
RETURNS event_trigger AS $$
BEGIN
END;
$$ LANGUAGE plpgsql;
