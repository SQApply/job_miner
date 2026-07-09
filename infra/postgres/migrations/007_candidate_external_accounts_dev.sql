-- Development-only external portal account storage for the application agent.
-- Run locally after migration 006:
--   docker exec -i job_miner_postgres psql -U job_miner_app -d job_miner_control < infra/postgres/migrations/007_candidate_external_accounts_dev.sql
--
-- IMPORTANT: password_plaintext_dev is intentionally plaintext for local demo only.
-- Replace this column with Azure Key Vault / AWS Secrets Manager references before production.

CREATE TABLE IF NOT EXISTS job_miner_control.candidate_external_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID REFERENCES job_miner_control.organizations(id),
    app_user_id UUID NOT NULL REFERENCES job_miner_control.app_users(id),
    candidate_id TEXT NOT NULL,
    portal_domain TEXT NOT NULL,
    username_email TEXT,
    password_plaintext_dev TEXT,
    allow_agent_login BOOLEAN NOT NULL DEFAULT TRUE,
    allow_agent_signup BOOLEAN NOT NULL DEFAULT FALSE,
    account_status TEXT NOT NULL DEFAULT 'active',
    last_login_on TIMESTAMPTZ,
    last_verified_on TIMESTAMPTZ,
    created_by_agent BOOLEAN NOT NULL DEFAULT FALSE,
    created_by UUID,
    created_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    modified_by UUID,
    modified_on TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_candidate_external_account UNIQUE (app_user_id, candidate_id, portal_domain)
);

CREATE INDEX IF NOT EXISTS idx_candidate_external_accounts_candidate
    ON job_miner_control.candidate_external_accounts (app_user_id, candidate_id, portal_domain);

INSERT INTO job_miner_control.application_portal_strategies (
    portal_domain, strategy_key, strategy_type, is_enabled, requires_login,
    supports_auto_submit, supports_resume_upload, max_concurrent_applications,
    rate_limit_per_minute, metadata
) VALUES (
    'careers.strategicstaff.com',
    'strategicstaff_direct_apply',
    'playwright_form_submit',
    TRUE,
    FALSE,
    TRUE,
    TRUE,
    1,
    6,
    '{"notes":"Focused demo strategy for Strategic Staffing Solutions careers pages."}'::jsonb
)
ON CONFLICT (portal_domain, strategy_key) DO UPDATE SET
    strategy_type = EXCLUDED.strategy_type,
    is_enabled = EXCLUDED.is_enabled,
    requires_login = EXCLUDED.requires_login,
    supports_auto_submit = EXCLUDED.supports_auto_submit,
    supports_resume_upload = EXCLUDED.supports_resume_upload,
    max_concurrent_applications = EXCLUDED.max_concurrent_applications,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
    metadata = job_miner_control.application_portal_strategies.metadata || EXCLUDED.metadata,
    modified_on = NOW();
