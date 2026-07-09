from __future__ import annotations

import json
import traceback as traceback_module
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_ORG_CODE = "default"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, default=str)


def _normalize_email(value: Any) -> str | None:
    email = str(value or "").strip().lower()
    return email or None


def _realm_from_issuer(issuer: str) -> str:
    return str(issuer or "").rstrip("/").split("/")[-1] or "job-miner"


class ControlRepository:
    """PostgreSQL control-plane repository.

    This repository stores users, identity mappings, tenant references,
    pipeline/task status, and candidate UI actions. MongoDB remains the system
    of record for resume, candidate tower, job tower, and matching documents.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_default_org_id(self) -> str:
        row = self.session.execute(
            text("SELECT id FROM job_miner_control.organizations WHERE organization_code = :code"),
            {"code": DEFAULT_ORG_CODE},
        ).mappings().first()
        if not row:
            raise RuntimeError("Default organization is missing. Run Postgres schema initialization first.")
        return str(row["id"])

    def resolve_organization_id_from_claims(self, claims: dict[str, Any]) -> str:
        """Resolve tenant from Keycloak groups, then fall back to the default tenant.

        Group format expected from Keycloak: /tenants/<organization_code>/...
        Example: /tenants/default/candidates
        """
        groups = [str(g) for g in claims.get("groups") or []]
        for group in groups:
            parts = [part for part in group.strip("/").split("/") if part]
            if len(parts) >= 2 and parts[0] == "tenants":
                row = self.session.execute(
                    text(
                        """
                        SELECT id
                        FROM job_miner_control.organizations
                        WHERE organization_code = :code AND is_active = TRUE
                        LIMIT 1
                        """
                    ),
                    {"code": parts[1]},
                ).mappings().first()
                if row:
                    return str(row["id"])
        return self.get_default_org_id()

    def upsert_user_from_token(self, claims: dict[str, Any]) -> dict[str, Any]:
        issuer = str(claims.get("iss") or "").rstrip("/")
        realm = _realm_from_issuer(issuer)
        keycloak_user_id = str(claims.get("sub") or "").strip()
        if not issuer:
            raise ValueError("JWT token is missing iss claim.")
        if not keycloak_user_id:
            raise ValueError("JWT token is missing sub claim.")

        org_id = self.resolve_organization_id_from_claims(claims)
        email = _normalize_email(claims.get("email"))
        email_verified = bool(claims.get("email_verified") or False)
        roles = extract_roles(claims)
        groups = [str(g) for g in claims.get("groups") or []]

        params = {
            "organization_id": org_id,
            "keycloak_realm": realm,
            "keycloak_user_id": keycloak_user_id,
            "issuer": issuer,
            "subject": keycloak_user_id,
            "identity_provider": self._identity_provider_from_claims(claims),
            "username": claims.get("preferred_username") or email,
            "email": email,
            "email_verified": email_verified,
            "full_name": claims.get("name"),
            "first_name": claims.get("given_name"),
            "last_name": claims.get("family_name"),
            "preferred_username": claims.get("preferred_username"),
            "metadata": _json({
                "raw_roles": roles,
                "groups": groups,
                "token_azp": claims.get("azp"),
                "token_aud": claims.get("aud"),
                "identity_provider": self._identity_provider_from_claims(claims),
            }),
        }

        app_user_id = self._find_user_id_by_identity(issuer, keycloak_user_id)
        resolution_strategy = "identity"

        if not app_user_id:
            # Backward compatibility for databases created before app_user_identities.
            app_user_id = self._find_legacy_user_id_by_keycloak(realm, keycloak_user_id)
            resolution_strategy = "legacy_keycloak"

        if not app_user_id and email and email_verified:
            app_user_id = self._find_user_id_by_verified_email(org_id, email)
            resolution_strategy = "verified_email_link"

        if not app_user_id and email and not email_verified and self._find_user_id_by_email(org_id, email):
            raise ValueError("Email already exists but the login email is not verified; refusing automatic identity link.")

        if app_user_id:
            row = self._update_app_user_from_claims(app_user_id, params)
            self._ensure_user_identity(app_user_id, params)
            if resolution_strategy == "verified_email_link":
                self.add_audit_event(
                    organization_id=org_id,
                    actor_app_user_id=app_user_id,
                    actor_keycloak_user_id=keycloak_user_id,
                    event_type="auth.identity.linked_by_verified_email",
                    entity_type="app_user",
                    entity_id=app_user_id,
                    after_payload={"issuer": issuer, "subject": keycloak_user_id, "email": email},
                )
        else:
            row = self._insert_app_user_from_claims(params)
            app_user_id = str(row["id"])
            self._ensure_user_identity(app_user_id, params)
            self.add_audit_event(
                organization_id=org_id,
                actor_app_user_id=app_user_id,
                actor_keycloak_user_id=keycloak_user_id,
                event_type="auth.user.created_from_keycloak_token",
                entity_type="app_user",
                entity_id=app_user_id,
                after_payload={"issuer": issuer, "subject": keycloak_user_id, "email": email},
            )

        self.sync_user_roles(str(row["id"]), roles, groups)
        out = dict(row)
        out["login_keycloak_user_id"] = keycloak_user_id
        out["login_issuer"] = issuer
        out["email_verified"] = email_verified
        return out

    def _identity_provider_from_claims(self, claims: dict[str, Any]) -> str:
        identity_provider = claims.get("identity_provider") or claims.get("kc_idp")
        if identity_provider:
            return str(identity_provider)
        federated = claims.get("federated_identity") or claims.get("idp")
        if federated:
            return str(federated)
        return "keycloak"

    def _find_user_id_by_identity(self, issuer: str, subject: str) -> str | None:
        row = self.session.execute(
            text(
                """
                SELECT app_user_id
                FROM job_miner_control.app_user_identities
                WHERE issuer = :issuer AND subject = :subject AND is_active = TRUE
                LIMIT 1
                """
            ),
            {"issuer": issuer, "subject": subject},
        ).mappings().first()
        return str(row["app_user_id"]) if row else None

    def _find_legacy_user_id_by_keycloak(self, realm: str, subject: str) -> str | None:
        row = self.session.execute(
            text(
                """
                SELECT id
                FROM job_miner_control.app_users
                WHERE keycloak_realm = :realm AND keycloak_user_id = :subject AND is_active = TRUE
                LIMIT 1
                """
            ),
            {"realm": realm, "subject": subject},
        ).mappings().first()
        return str(row["id"]) if row else None

    def _find_user_id_by_verified_email(self, organization_id: str, email: str) -> str | None:
        rows = self.session.execute(
            text(
                """
                SELECT id
                FROM job_miner_control.app_users
                WHERE organization_id = :organization_id
                  AND lower(trim(email)) = :email
                  AND is_active = TRUE
                ORDER BY created_on ASC
                LIMIT 2
                """
            ),
            {"organization_id": organization_id, "email": email},
        ).mappings().all()
        if len(rows) > 1:
            raise ValueError("More than one active verified user exists for this organization/email.")
        return str(rows[0]["id"]) if rows else None

    def _find_user_id_by_email(self, organization_id: str, email: str) -> str | None:
        rows = self.session.execute(
            text(
                """
                SELECT id
                FROM job_miner_control.app_users
                WHERE organization_id = :organization_id
                  AND lower(trim(email)) = :email
                  AND is_active = TRUE
                ORDER BY created_on ASC
                LIMIT 2
                """
            ),
            {"organization_id": organization_id, "email": email},
        ).mappings().all()
        if len(rows) > 1:
            raise ValueError("More than one active user exists for this organization/email.")
        return str(rows[0]["id"]) if rows else None

    def _insert_app_user_from_claims(self, params: dict[str, Any]) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.app_users (
                    organization_id, keycloak_realm, keycloak_user_id, username, email,
                    email_verified, full_name, first_name, last_name, preferred_username,
                    last_login_on, last_token_seen_on, metadata
                )
                VALUES (
                    :organization_id, :keycloak_realm, :keycloak_user_id, :username, :email,
                    :email_verified, :full_name, :first_name, :last_name, :preferred_username,
                    NOW(), NOW(), CAST(:metadata AS jsonb)
                )
                RETURNING id, organization_id, keycloak_realm, keycloak_user_id, email,
                          email_verified, full_name, preferred_username, status
                """
            ),
            params,
        ).mappings().one()
        return dict(row)

    def _update_app_user_from_claims(self, app_user_id: str, params: dict[str, Any]) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.app_users
                SET organization_id = :organization_id,
                    keycloak_realm = :keycloak_realm,
                    keycloak_user_id = :keycloak_user_id,
                    username = :username,
                    email = COALESCE(:email, email),
                    email_verified = CASE WHEN :email IS NULL THEN email_verified ELSE :email_verified END,
                    full_name = COALESCE(:full_name, full_name),
                    first_name = COALESCE(:first_name, first_name),
                    last_name = COALESCE(:last_name, last_name),
                    preferred_username = COALESCE(:preferred_username, preferred_username),
                    last_login_on = NOW(),
                    last_token_seen_on = NOW(),
                    modified_on = NOW(),
                    metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:metadata AS jsonb)
                WHERE id = :app_user_id
                RETURNING id, organization_id, keycloak_realm, keycloak_user_id, email,
                          email_verified, full_name, preferred_username, status
                """
            ),
            {**params, "app_user_id": app_user_id},
        ).mappings().one()
        return dict(row)

    def _ensure_user_identity(self, app_user_id: str, params: dict[str, Any]) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.app_user_identities (
                    organization_id, app_user_id, issuer, subject, keycloak_realm,
                    identity_provider, email, email_verified, first_seen_on, last_seen_on, metadata
                )
                VALUES (
                    :organization_id, :app_user_id, :issuer, :subject, :keycloak_realm,
                    :identity_provider, :email, :email_verified, NOW(), NOW(), CAST(:metadata AS jsonb)
                )
                ON CONFLICT (issuer, subject)
                DO UPDATE SET
                    app_user_id = EXCLUDED.app_user_id,
                    organization_id = EXCLUDED.organization_id,
                    identity_provider = EXCLUDED.identity_provider,
                    email = EXCLUDED.email,
                    email_verified = EXCLUDED.email_verified,
                    last_seen_on = NOW(),
                    modified_on = NOW(),
                    metadata = COALESCE(job_miner_control.app_user_identities.metadata, '{}'::jsonb) || EXCLUDED.metadata,
                    is_active = TRUE
                """
            ),
            {**params, "app_user_id": app_user_id},
        )

    def sync_user_roles(self, app_user_id: str, roles: list[str], groups: list[str]) -> None:
        # Clear only Keycloak-sourced assignments. Manual overrides remain untouched.
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.app_user_role_assignments
                SET is_active = FALSE, modified_on = NOW()
                WHERE app_user_id = :user_id AND assignment_source IN ('keycloak_role', 'keycloak_group')
                """
            ),
            {"user_id": app_user_id},
        )

        for role in roles:
            mapped = self.session.execute(
                text(
                    """
                    SELECT app_role_id
                    FROM job_miner_control.keycloak_role_mappings
                    WHERE is_active = TRUE AND keycloak_role_name = :role
                    ORDER BY priority ASC
                    LIMIT 1
                    """
                ),
                {"role": role},
            ).scalar_one_or_none()
            if mapped:
                self.session.execute(
                    text(
                        """
                        INSERT INTO job_miner_control.app_user_role_assignments (
                            app_user_id, app_role_id, assignment_source, keycloak_role_name
                        ) VALUES (:user_id, :role_id, 'keycloak_role', :role)
                        ON CONFLICT (app_user_id, app_role_id, assignment_source, keycloak_role_name, keycloak_group_path)
                        DO UPDATE SET is_active = TRUE, modified_on = NOW()
                        """
                    ),
                    {"user_id": app_user_id, "role_id": str(mapped), "role": role},
                )

        for group in groups:
            mapped = self.session.execute(
                text(
                    """
                    SELECT app_role_id
                    FROM job_miner_control.keycloak_role_mappings
                    WHERE is_active = TRUE AND keycloak_group_path = :group
                    ORDER BY priority ASC
                    LIMIT 1
                    """
                ),
                {"group": group},
            ).scalar_one_or_none()
            if mapped:
                self.session.execute(
                    text(
                        """
                        INSERT INTO job_miner_control.app_user_role_assignments (
                            app_user_id, app_role_id, assignment_source, keycloak_group_path
                        ) VALUES (:user_id, :role_id, 'keycloak_group', :group)
                        ON CONFLICT (app_user_id, app_role_id, assignment_source, keycloak_role_name, keycloak_group_path)
                        DO UPDATE SET is_active = TRUE, modified_on = NOW()
                        """
                    ),
                    {"user_id": app_user_id, "role_id": str(mapped), "group": group},
                )

    def add_audit_event(
        self,
        *,
        organization_id: str | None,
        actor_app_user_id: str | None,
        actor_keycloak_user_id: str | None,
        event_type: str,
        entity_type: str,
        entity_id: str | None,
        before_payload: dict[str, Any] | None = None,
        after_payload: dict[str, Any] | None = None,
    ) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.audit_events (
                    organization_id, actor_app_user_id, actor_keycloak_user_id,
                    event_type, entity_type, entity_id, before_payload, after_payload
                ) VALUES (
                    :organization_id, :actor_app_user_id, :actor_keycloak_user_id,
                    :event_type, :entity_type, :entity_id,
                    CAST(:before_payload AS jsonb), CAST(:after_payload AS jsonb)
                )
                """
            ),
            {
                "organization_id": organization_id,
                "actor_app_user_id": actor_app_user_id,
                "actor_keycloak_user_id": actor_keycloak_user_id,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "before_payload": _json(before_payload or {}),
                "after_payload": _json(after_payload or {}),
            },
        )

    def user_has_permission(self, app_user_id: str, permission_key: str) -> bool:
        value = self.session.execute(
            text(
                """
                SELECT 1
                FROM job_miner_control.app_user_role_assignments ura
                JOIN job_miner_control.app_role_permissions rp ON rp.role_id = ura.app_role_id
                JOIN job_miner_control.app_permissions p ON p.id = rp.permission_id
                WHERE ura.app_user_id = :user_id
                  AND ura.is_active = TRUE
                  AND p.permission_key = :permission_key
                LIMIT 1
                """
            ),
            {"user_id": app_user_id, "permission_key": permission_key},
        ).scalar_one_or_none()
        return value is not None

    def get_primary_candidate_link(self, app_user_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                SELECT * FROM job_miner_control.candidate_user_links
                WHERE app_user_id = :user_id AND is_primary = TRUE AND is_active = TRUE
                ORDER BY created_on DESC LIMIT 1
                """
            ),
            {"user_id": app_user_id},
        ).mappings().first()
        return dict(row) if row else None

    def link_candidate(
        self,
        app_user_id: str,
        candidate_id: str,
        resume_id: str | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        org_id = self.get_default_org_id()
        # Ensure only one active primary candidate link exists for a user.
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.candidate_user_links
                SET is_primary = FALSE, modified_on = NOW()
                WHERE app_user_id = :user_id AND is_primary = TRUE AND candidate_id <> :candidate_id
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id},
        )
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_user_links (
                    organization_id, app_user_id, candidate_id, resume_id, is_primary, metadata
                ) VALUES (:org_id, :user_id, :candidate_id, :resume_id, TRUE, CAST(:metadata AS jsonb))
                ON CONFLICT (app_user_id, candidate_id)
                DO UPDATE SET
                    resume_id = EXCLUDED.resume_id,
                    is_primary = TRUE,
                    modified_on = NOW(),
                    is_active = TRUE,
                    metadata = COALESCE(job_miner_control.candidate_user_links.metadata, '{}'::jsonb) || EXCLUDED.metadata
                RETURNING *
                """
            ),
            {
                "org_id": org_id,
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "resume_id": resume_id,
                "metadata": _json(metadata or {}),
            },
        ).mappings().one()
        return dict(row)

    def save_job(self, app_user_id: str, candidate_id: str, job_id: str, *, match_run_id: str | None, source_collection: str | None, status: str) -> dict[str, Any]:
        org_id = self.get_default_org_id()
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_saved_jobs (
                    organization_id, app_user_id, candidate_id, job_id, match_run_id, source_collection, status
                ) VALUES (:org_id, :user_id, :candidate_id, :job_id, :match_run_id, :source_collection, :status)
                ON CONFLICT (app_user_id, candidate_id, job_id)
                DO UPDATE SET status = EXCLUDED.status, match_run_id = EXCLUDED.match_run_id,
                              source_collection = EXCLUDED.source_collection, modified_on = NOW(), is_active = TRUE
                RETURNING *
                """
            ),
            {
                "org_id": org_id,
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "match_run_id": match_run_id,
                "source_collection": source_collection,
                "status": status,
            },
        ).mappings().one()
        return dict(row)

    def create_or_update_application(self, app_user_id: str, candidate_id: str, job_id: str, *, match_run_id: str | None, apply_url: str | None, status: str) -> dict[str, Any]:
        org_id = self.get_default_org_id()
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_job_applications (
                    organization_id, app_user_id, candidate_id, job_id, match_run_id, application_status, apply_url, last_status_on
                ) VALUES (:org_id, :user_id, :candidate_id, :job_id, :match_run_id, :status, :apply_url, NOW())
                ON CONFLICT (app_user_id, candidate_id, job_id)
                DO UPDATE SET application_status = EXCLUDED.application_status,
                              match_run_id = EXCLUDED.match_run_id,
                              apply_url = EXCLUDED.apply_url,
                              last_status_on = NOW(), modified_on = NOW(), is_active = TRUE
                RETURNING *
                """
            ),
            {
                "org_id": org_id,
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "match_run_id": match_run_id,
                "apply_url": apply_url,
                "status": status,
            },
        ).mappings().one()
        return dict(row)

    def add_application_event(self, *, app_user_id: str, candidate_id: str, job_id: str, event_type: str, message: str | None = None, payload: dict[str, Any] | None = None, application_id: str | None = None) -> None:
        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.application_events (
                    organization_id, application_id, app_user_id, candidate_id, job_id, event_type, message, event_payload
                ) VALUES (:org_id, :application_id, :user_id, :candidate_id, :job_id, :event_type, :message, CAST(:payload AS jsonb))
                """
            ),
            {
                "org_id": self.get_default_org_id(),
                "application_id": application_id,
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "event_type": event_type,
                "message": message,
                "payload": _json(payload or {}),
            },
        )

    def list_saved_jobs(self, app_user_id: str, candidate_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT * FROM job_miner_control.candidate_saved_jobs
                WHERE app_user_id = :user_id AND candidate_id = :candidate_id AND is_active = TRUE
                ORDER BY modified_on DESC
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id},
        ).mappings().all()
        return [dict(r) for r in rows]

    def remove_saved_job(self, app_user_id: str, candidate_id: str, job_id: str) -> dict[str, Any] | None:
        """Soft-remove a job from the candidate saved-jobs list.

        The application/history rows are intentionally left untouched. Re-saving the
        same job later reactivates the saved-job row through save_job().
        """
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.candidate_saved_jobs
                SET status = 'removed_from_saved',
                    is_active = FALSE,
                    modified_on = NOW()
                WHERE app_user_id = :user_id
                  AND candidate_id = :candidate_id
                  AND job_id = :job_id
                  AND is_active = TRUE
                RETURNING *
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id, "job_id": job_id},
        ).mappings().first()
        return dict(row) if row else None

    def list_applications(self, app_user_id: str, candidate_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT * FROM job_miner_control.candidate_job_applications
                WHERE app_user_id = :user_id AND candidate_id = :candidate_id AND is_active = TRUE
                ORDER BY modified_on DESC
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id},
        ).mappings().all()
        return [dict(r) for r in rows]

    def list_agent_eligible_saved_jobs(
        self,
        app_user_id: str,
        candidate_id: str,
        *,
        job_ids: list[str] | None = None,
        max_jobs: int = 25,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user_id": app_user_id,
            "candidate_id": candidate_id,
            "max_jobs": max(1, min(int(max_jobs or 25), 100)),
        }
        job_filter = ""
        if job_ids:
            placeholders: list[str] = []
            for index, job_id in enumerate(dict.fromkeys(str(j).strip() for j in job_ids if str(j).strip())):
                key = f"job_id_{index}"
                placeholders.append(f":{key}")
                params[key] = job_id
            if placeholders:
                job_filter = f"AND sj.job_id IN ({', '.join(placeholders)})"

        rows = self.session.execute(
            text(
                f"""
                SELECT
                    sj.*,
                    app.id AS existing_application_id,
                    app.application_status AS existing_application_status,
                    app.apply_url AS existing_application_apply_url
                FROM job_miner_control.candidate_saved_jobs sj
                LEFT JOIN job_miner_control.candidate_job_applications app
                    ON app.app_user_id = sj.app_user_id
                   AND app.candidate_id = sj.candidate_id
                   AND app.job_id = sj.job_id
                   AND app.is_active = TRUE
                WHERE sj.app_user_id = :user_id
                  AND sj.candidate_id = :candidate_id
                  AND sj.is_active = TRUE
                  AND sj.status = 'saved'
                  {job_filter}
                ORDER BY sj.modified_on DESC
                LIMIT :max_jobs
                """
            ),
            params,
        ).mappings().all()
        return [dict(r) for r in rows]

    def get_active_application_batch_for_candidate(self, app_user_id: str, candidate_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.candidate_application_batches
                WHERE app_user_id = :user_id
                  AND candidate_id = :candidate_id
                  AND is_active = TRUE
                  AND batch_status IN ('queued', 'running')
                ORDER BY created_on DESC
                LIMIT 1
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id},
        ).mappings().first()
        return dict(row) if row else None

    def create_application_batch(
        self,
        *,
        app_user_id: str,
        candidate_id: str,
        requested_job_count: int,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_application_batches (
                    organization_id, app_user_id, candidate_id, batch_status,
                    requested_job_count, queued_job_count, metadata
                ) VALUES (
                    :org_id, :user_id, :candidate_id, 'queued',
                    :requested_job_count, :queued_job_count, CAST(:metadata AS jsonb)
                )
                RETURNING *
                """
            ),
            {
                "org_id": self.get_default_org_id(),
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "requested_job_count": int(requested_job_count or 0),
                "queued_job_count": int(requested_job_count or 0),
                "metadata": _json(metadata or {}),
            },
        ).mappings().one()
        return dict(row)

    def set_application_batch_task(self, batch_id: str, task_uuid: str, *, langgraph_thread_id: str | None = None) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.candidate_application_batches
                SET celery_task_uuid = :task_uuid,
                    langgraph_thread_id = COALESCE(:langgraph_thread_id, langgraph_thread_id),
                    modified_on = NOW()
                WHERE id = :batch_id
                """
            ),
            {"batch_id": batch_id, "task_uuid": task_uuid, "langgraph_thread_id": langgraph_thread_id},
        )

    def get_application_batch(self, batch_id: str, *, app_user_id: str | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {"batch_id": batch_id}
        user_filter = ""
        if app_user_id:
            user_filter = "AND app_user_id = :user_id"
            params["user_id"] = app_user_id
        row = self.session.execute(
            text(
                f"""
                SELECT *
                FROM job_miner_control.candidate_application_batches
                WHERE id = :batch_id AND is_active = TRUE {user_filter}
                LIMIT 1
                """
            ),
            params,
        ).mappings().first()
        return dict(row) if row else None

    def list_application_batches(self, app_user_id: str, candidate_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.candidate_application_batches
                WHERE app_user_id = :user_id
                  AND candidate_id = :candidate_id
                  AND is_active = TRUE
                ORDER BY created_on DESC
                LIMIT :limit
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id, "limit": max(1, min(int(limit or 10), 50))},
        ).mappings().all()
        return [dict(r) for r in rows]

    def update_application_batch_status(
        self,
        batch_id: str,
        status: str,
        *,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        started_expr = "started_on = COALESCE(started_on, NOW())," if status == "running" else ""
        completed_expr = "completed_on = COALESCE(completed_on, NOW())," if status in {"completed", "completed_with_failures", "failed", "cancelled"} else ""
        row = self.session.execute(
            text(
                f"""
                UPDATE job_miner_control.candidate_application_batches
                SET batch_status = :status,
                    {started_expr}
                    {completed_expr}
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :batch_id
                RETURNING *
                """
            ),
            {
                "batch_id": batch_id,
                "status": status,
                "metadata": _json({"error_message": error_message} if error_message else (metadata or {})),
            },
        ).mappings().first()
        return dict(row) if row else None

    def create_application_job_run(
        self,
        *,
        batch_id: str,
        app_user_id: str,
        candidate_id: str,
        job_id: str,
        application_id: str | None,
        apply_url: str | None,
        portal_domain: str | None,
        apply_strategy: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_application_job_runs (
                    batch_id, organization_id, app_user_id, candidate_id, job_id,
                    application_id, run_status, apply_url, portal_domain, apply_strategy, metadata
                ) VALUES (
                    :batch_id, :org_id, :user_id, :candidate_id, :job_id,
                    :application_id, 'queued', :apply_url, :portal_domain, :apply_strategy, CAST(:metadata AS jsonb)
                )
                ON CONFLICT (batch_id, app_user_id, candidate_id, job_id)
                DO UPDATE SET
                    application_id = COALESCE(EXCLUDED.application_id, job_miner_control.candidate_application_job_runs.application_id),
                    apply_url = COALESCE(EXCLUDED.apply_url, job_miner_control.candidate_application_job_runs.apply_url),
                    portal_domain = COALESCE(EXCLUDED.portal_domain, job_miner_control.candidate_application_job_runs.portal_domain),
                    apply_strategy = COALESCE(EXCLUDED.apply_strategy, job_miner_control.candidate_application_job_runs.apply_strategy),
                    metadata = job_miner_control.candidate_application_job_runs.metadata || EXCLUDED.metadata,
                    modified_on = NOW(),
                    is_active = TRUE
                RETURNING *
                """
            ),
            {
                "batch_id": batch_id,
                "org_id": self.get_default_org_id(),
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "job_id": job_id,
                "application_id": application_id,
                "apply_url": apply_url,
                "portal_domain": portal_domain,
                "apply_strategy": apply_strategy,
                "metadata": _json(metadata or {}),
            },
        ).mappings().one()
        return dict(row)

    def set_application_job_run_task(self, job_run_id: str, task_uuid: str, *, langgraph_thread_id: str | None = None) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.candidate_application_job_runs
                SET celery_task_uuid = :task_uuid,
                    langgraph_thread_id = COALESCE(:langgraph_thread_id, langgraph_thread_id),
                    modified_on = NOW()
                WHERE id = :job_run_id
                """
            ),
            {"job_run_id": job_run_id, "task_uuid": task_uuid, "langgraph_thread_id": langgraph_thread_id},
        )

    def get_application_job_run(self, job_run_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.candidate_application_job_runs
                WHERE id = :job_run_id AND is_active = TRUE
                LIMIT 1
                """
            ),
            {"job_run_id": job_run_id},
        ).mappings().first()
        return dict(row) if row else None

    def list_application_job_runs(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.candidate_application_job_runs
                WHERE batch_id = :batch_id AND is_active = TRUE
                ORDER BY created_on ASC
                """
            ),
            {"batch_id": batch_id},
        ).mappings().all()
        return [dict(r) for r in rows]

    def update_application_job_run_status(
        self,
        job_run_id: str,
        status: str,
        *,
        apply_strategy: str | None = None,
        portal_domain: str | None = None,
        error_type: str | None = None,
        error_message: str | None = None,
        external_confirmation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        started_expr = "started_on = COALESCE(started_on, NOW())," if status == "running" else ""
        completed_expr = "completed_on = COALESCE(completed_on, NOW())," if status in {"submitted", "failed", "needs_review", "precheck_failed", "blocked_external_login", "blocked_captcha", "unsupported_portal", "skipped_duplicate"} else ""
        submitted_expr = "submitted_on = COALESCE(submitted_on, NOW())," if status == "submitted" else ""
        row = self.session.execute(
            text(
                f"""
                UPDATE job_miner_control.candidate_application_job_runs
                SET run_status = :status,
                    {started_expr}
                    {completed_expr}
                    {submitted_expr}
                    apply_strategy = COALESCE(:apply_strategy, apply_strategy),
                    portal_domain = COALESCE(:portal_domain, portal_domain),
                    error_type = :error_type,
                    error_message = :error_message,
                    external_confirmation_id = COALESCE(:external_confirmation_id, external_confirmation_id),
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :job_run_id
                RETURNING *
                """
            ),
            {
                "job_run_id": job_run_id,
                "status": status,
                "apply_strategy": apply_strategy,
                "portal_domain": portal_domain,
                "error_type": error_type,
                "error_message": error_message,
                "external_confirmation_id": external_confirmation_id,
                "metadata": _json(metadata or {}),
            },
        ).mappings().first()
        return dict(row) if row else None

    def update_application_status_by_id(
        self,
        application_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        applied: bool = False,
    ) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                UPDATE job_miner_control.candidate_job_applications
                SET application_status = :status,
                    last_status_on = NOW(),
                    applied_on = CASE WHEN :applied THEN COALESCE(applied_on, NOW()) ELSE applied_on END,
                    agent_enabled = TRUE,
                    metadata = metadata || CAST(:metadata AS jsonb),
                    modified_on = NOW()
                WHERE id = :application_id
                RETURNING *
                """
            ),
            {"application_id": application_id, "status": status, "metadata": _json(metadata or {}), "applied": applied},
        ).mappings().first()
        return dict(row) if row else None

    def refresh_application_batch_counts(self, batch_id: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text(
                """
                WITH counts AS (
                    SELECT
                        COUNT(*) FILTER (WHERE run_status = 'queued') AS queued_count,
                        COUNT(*) FILTER (WHERE run_status = 'running') AS running_count,
                        COUNT(*) FILTER (WHERE run_status = 'submitted') AS success_count,
                        COUNT(*) FILTER (WHERE run_status IN ('failed', 'precheck_failed', 'blocked_external_login', 'blocked_captcha', 'unsupported_portal')) AS failed_count,
                        COUNT(*) FILTER (WHERE run_status = 'needs_review') AS needs_review_count,
                        COUNT(*) FILTER (WHERE run_status = 'skipped_duplicate') AS skipped_count,
                        COUNT(*) AS total_count
                    FROM job_miner_control.candidate_application_job_runs
                    WHERE batch_id = :batch_id AND is_active = TRUE
                ), final_status AS (
                    SELECT
                        CASE
                            WHEN total_count = 0 THEN 'completed'
                            WHEN queued_count > 0 OR running_count > 0 THEN 'running'
                            WHEN failed_count > 0 OR needs_review_count > 0 THEN 'completed_with_failures'
                            ELSE 'completed'
                        END AS batch_status,
                        *
                    FROM counts
                )
                UPDATE job_miner_control.candidate_application_batches b
                SET queued_job_count = final_status.queued_count,
                    running_job_count = final_status.running_count,
                    success_count = final_status.success_count,
                    failed_count = final_status.failed_count,
                    needs_review_count = final_status.needs_review_count,
                    skipped_count = final_status.skipped_count,
                    batch_status = final_status.batch_status,
                    completed_on = CASE
                        WHEN final_status.batch_status IN ('completed', 'completed_with_failures') THEN COALESCE(b.completed_on, NOW())
                        ELSE b.completed_on
                    END,
                    modified_on = NOW()
                FROM final_status
                WHERE b.id = :batch_id
                RETURNING b.*
                """
            ),
            {"batch_id": batch_id},
        ).mappings().first()
        return dict(row) if row else None

    def create_candidate_notification(
        self,
        *,
        app_user_id: str,
        candidate_id: str,
        notification_type: str,
        title: str,
        body: str | None,
        payload: dict[str, Any] | None = None,
        channel: str = "candidate_portal",
        status: str = "visible",
    ) -> dict[str, Any]:
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.candidate_notifications (
                    organization_id, app_user_id, candidate_id, notification_type,
                    channel, status, title, body, payload
                ) VALUES (
                    :org_id, :user_id, :candidate_id, :notification_type,
                    :channel, :status, :title, :body, CAST(:payload AS jsonb)
                )
                RETURNING *
                """
            ),
            {
                "org_id": self.get_default_org_id(),
                "user_id": app_user_id,
                "candidate_id": candidate_id,
                "notification_type": notification_type,
                "channel": channel,
                "status": status,
                "title": title,
                "body": body,
                "payload": _json(payload or {}),
            },
        ).mappings().one()
        return dict(row)

    def list_candidate_notifications(self, app_user_id: str, candidate_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT *
                FROM job_miner_control.candidate_notifications
                WHERE app_user_id = :user_id
                  AND candidate_id = :candidate_id
                  AND is_active = TRUE
                ORDER BY created_on DESC
                LIMIT :limit
                """
            ),
            {"user_id": app_user_id, "candidate_id": candidate_id, "limit": max(1, min(int(limit or 10), 50))},
        ).mappings().all()
        return [dict(r) for r in rows]

    def create_pipeline_run(self, *, pipeline_name: str, user: dict[str, Any] | None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        run_session_id = f"{pipeline_name}_{utc_now().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
        org_id = self.get_default_org_id()
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.pipeline_runs (
                    organization_id, run_session_id, pipeline_name, status, triggered_by_app_user_id,
                    triggered_by_keycloak_user_id, metadata
                ) VALUES (:org_id, :run_session_id, :pipeline_name, 'queued', :app_user_id, :kc_user_id, CAST(:metadata AS jsonb))
                RETURNING *
                """
            ),
            {
                "org_id": org_id,
                "run_session_id": run_session_id,
                "pipeline_name": pipeline_name,
                "app_user_id": user.get("id") if user else None,
                "kc_user_id": user.get("keycloak_user_id") if user else None,
                "metadata": _json(metadata or {}),
            },
        ).mappings().one()
        return dict(row)

    def create_task_row(self, *, task_uuid: str, task_name: str, queue_name: str, pipeline_run_id: str | None, user: dict[str, Any] | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        user_id = user.get("id") if user else None
        row = self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.celery_tasks (
                    organization_id, task_uuid, task_name, queue_name, pipeline_run_id,
                    requested_by_app_user_id, payload, created_by, modified_by
                ) VALUES (
                    :org_id, :task_uuid, :task_name, :queue_name, :pipeline_run_id,
                    :user_id, CAST(:payload AS jsonb), :user_id, :user_id
                )
                ON CONFLICT (task_uuid) DO UPDATE
                SET modified_on = NOW(),
                    modified_by = EXCLUDED.modified_by
                RETURNING *
                """
            ),
            {
                "org_id": str((user or {}).get("organization_id") or self.get_default_org_id()),
                "task_uuid": task_uuid,
                "task_name": task_name,
                "queue_name": queue_name,
                "pipeline_run_id": pipeline_run_id,
                "user_id": user_id,
                "payload": _json(payload or {}),
            },
        ).mappings().one()
        return dict(row)

    def update_task_status(self, task_uuid: str, status: str, *, result: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        params = {
            "task_uuid": task_uuid,
            "status": status,
            "result": _json(result or {}),
            "error_type": type(error).__name__ if error else None,
            "error_message": str(error) if error else None,
            "traceback": traceback_module.format_exc() if error else None,
        }
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.celery_tasks
                SET status = :status,
                    result = CAST(:result AS jsonb),
                    error_type = :error_type,
                    error_message = :error_message,
                    traceback = :traceback,
                    started_on = CASE WHEN :status = 'running' AND started_on IS NULL THEN NOW() ELSE started_on END,
                    completed_on = CASE WHEN :status IN ('completed', 'failed') THEN NOW() ELSE completed_on END,
                    modified_on = NOW()
                WHERE task_uuid = :task_uuid
                """
            ),
            params,
        )

    def add_task_event(self, task_uuid: str, event_type: str, message: str | None = None, *, progress_percent: float | None = None, payload: dict[str, Any] | None = None) -> None:
        task_id = self.session.execute(
            text("SELECT id FROM job_miner_control.celery_tasks WHERE task_uuid = :task_uuid"),
            {"task_uuid": task_uuid},
        ).scalar_one_or_none()
        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.celery_task_events (
                    celery_task_id, event_type, message, progress_percent, event_payload
                ) VALUES (:task_id, :event_type, :message, :progress_percent, CAST(:payload AS jsonb))
                """
            ),
            {
                "task_id": str(task_id) if task_id else None,
                "event_type": event_type,
                "message": message,
                "progress_percent": progress_percent,
                "payload": _json(payload or {}),
            },
        )

    def record_processing_failure(
        self,
        *,
        failure_id: str,
        task_uuid: str | None,
        entity_type: str | None,
        entity_id: str | None,
        error: BaseException,
        failed_payload: dict[str, Any] | None = None,
    ) -> None:
        celery_task_id = None
        organization_id = self.get_default_org_id()
        if task_uuid:
            row = self.session.execute(
                text("SELECT id, organization_id FROM job_miner_control.celery_tasks WHERE task_uuid = :task_uuid"),
                {"task_uuid": task_uuid},
            ).mappings().first()
            if row:
                celery_task_id = str(row["id"])
                organization_id = str(row["organization_id"]) if row.get("organization_id") else organization_id

        self.session.execute(
            text(
                """
                INSERT INTO job_miner_control.processing_failures (
                    organization_id, celery_task_id, failure_id, entity_type, entity_id,
                    error_type, error_message, traceback, failed_payload
                ) VALUES (
                    :organization_id, :celery_task_id, :failure_id, :entity_type, :entity_id,
                    :error_type, :error_message, :traceback, CAST(:failed_payload AS jsonb)
                )
                ON CONFLICT (failure_id) DO NOTHING
                """
            ),
            {
                "organization_id": organization_id,
                "celery_task_id": celery_task_id,
                "failure_id": failure_id,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "error_type": type(error).__name__,
                "error_message": str(error),
                "traceback": traceback_module.format_exc(),
                "failed_payload": _json(failed_payload or {}),
            },
        )

    def complete_pipeline_run(self, pipeline_run_id: str, status: str, *, metrics: dict[str, Any] | None = None, error_message: str | None = None) -> None:
        self.session.execute(
            text(
                """
                UPDATE job_miner_control.pipeline_runs
                SET status = :status,
                    completed_on = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_on, created_on))),
                    metrics = CAST(:metrics AS jsonb),
                    error_message = :error_message,
                    modified_on = NOW()
                WHERE id = :pipeline_run_id
                """
            ),
            {"pipeline_run_id": pipeline_run_id, "status": status, "metrics": _json(metrics or {}), "error_message": error_message},
        )

    def start_pipeline_run(self, pipeline_run_id: str) -> None:
        self.session.execute(
            text(
                "UPDATE job_miner_control.pipeline_runs SET status = 'running', started_on = COALESCE(started_on, NOW()), modified_on = NOW() WHERE id = :id"
            ),
            {"id": pipeline_run_id},
        )

    def list_pipeline_runs(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.session.execute(
            text(
                """
                SELECT * FROM job_miner_control.pipeline_runs
                ORDER BY created_on DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
        return [dict(r) for r in rows]

    def get_task(self, task_uuid: str) -> dict[str, Any] | None:
        row = self.session.execute(
            text("SELECT * FROM job_miner_control.celery_tasks WHERE task_uuid = :task_uuid"),
            {"task_uuid": task_uuid},
        ).mappings().first()
        return dict(row) if row else None


def extract_roles(claims: dict[str, Any]) -> list[str]:
    roles: set[str] = set()
    realm_access = claims.get("realm_access") or {}
    roles.update(str(r) for r in realm_access.get("roles") or [])
    resource_access = claims.get("resource_access") or {}
    for client_payload in resource_access.values():
        roles.update(str(r) for r in (client_payload or {}).get("roles") or [])
    return sorted(roles)
