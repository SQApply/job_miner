-- Production authentication hardening for existing databases.
-- Run manually for already-created Postgres volumes:
--   docker exec -i job_miner_postgres psql -U job_miner_app -d job_miner_control < infra/postgres/migrations/002_auth_production_hardening.sql

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS job_miner_control;

ALTER TABLE job_miner_control.app_users
    ADD COLUMN IF NOT EXISTS normalized_email TEXT GENERATED ALWAYS AS (lower(NULLIF(btrim(email), ''))) STORED;

ALTER TABLE job_miner_control.app_users
    DROP CONSTRAINT IF EXISTS uq_app_user_org_email;

CREATE UNIQUE INDEX IF NOT EXISTS uq_app_user_org_normalized_email
    ON job_miner_control.app_users (organization_id, normalized_email)
    WHERE normalized_email IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_app_users_normalized_email
    ON job_miner_control.app_users (organization_id, normalized_email);

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

CREATE INDEX IF NOT EXISTS idx_app_user_identities_user
    ON job_miner_control.app_user_identities (app_user_id);

CREATE INDEX IF NOT EXISTS idx_app_user_identities_email
    ON job_miner_control.app_user_identities (organization_id, lower(trim(email)))
    WHERE email IS NOT NULL;

INSERT INTO job_miner_control.app_user_identities (
    organization_id, app_user_id, issuer, subject, keycloak_realm,
    identity_provider, email, email_verified, first_seen_on, last_seen_on, metadata
)
SELECT
    au.organization_id,
    au.id,
    COALESCE(krc.issuer_url, 'http://localhost:8080/realms/' || au.keycloak_realm) AS issuer,
    au.keycloak_user_id AS subject,
    au.keycloak_realm,
    'keycloak' AS identity_provider,
    au.email,
    au.email_verified,
    COALESCE(au.created_on, NOW()),
    COALESCE(au.last_token_seen_on, au.modified_on, NOW()),
    jsonb_build_object('backfilled_from', 'app_users')
FROM job_miner_control.app_users au
LEFT JOIN job_miner_control.keycloak_realm_configs krc ON krc.realm_name = au.keycloak_realm
WHERE au.keycloak_user_id IS NOT NULL
ON CONFLICT (issuer, subject) DO NOTHING;

CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_primary_link_per_user
    ON job_miner_control.candidate_user_links (app_user_id)
    WHERE is_primary = TRUE AND is_active = TRUE;

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
