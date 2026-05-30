# PostgreSQL Table Design: Job Miner MVP with Keycloak

## Database role

PostgreSQL is the control-plane database. MongoDB remains the domain warehouse for jobs, resumes, towers, matches, feedback, and training pairs. Qdrant stores vectors. Redis stores temporary Celery broker messages. Keycloak owns authentication and user lifecycle.

## Standard columns

Every PostgreSQL table includes a UUID primary key and standard operational fields wherever applicable: `organization_id`, `created_by`, `created_on`, `modified_by`, `modified_on`, `is_active`, and `metadata`.

## Tenant and Keycloak tables

### organizations
Represents a tenant/company. The current MVP creates one tenant named `default`, but the design supports many tenants later. Related to app users, pipeline runs, tasks, saved jobs, and application events.

### keycloak_realm_configs
Stores application-side Keycloak realm URLs: issuer, JWKS, token, authorization, userinfo, and admin API base URLs. The app validates JWTs against these values.

### keycloak_client_configs
Stores application-side Keycloak client references such as `job-miner-web`, `job-miner-api`, and future service clients. This is not Keycloak’s internal client table; it is a control-plane reference.

### app_users
Shadow user table for Keycloak users. Stores Keycloak `sub`, email, name, realm, status, and last token seen. It does not store passwords or MFA secrets.

### app_roles
Application roles such as `platform_admin`, `tenant_admin`, `candidate`, `recruiter`, and `viewer`.

### app_permissions
Granular permissions such as `pipeline.run`, `pipeline.view`, `candidate.view_self`, `recommendations.view_self`, `applications.manage_self`, and `feedback.create_self`.

### app_role_permissions
Maps app roles to app permissions.

### keycloak_role_mappings
Maps Keycloak realm/client roles or groups to local application roles. This avoids hardcoding role mapping in the API.

### app_user_role_assignments
Stores effective app role assignments for a user. Keycloak-sourced assignments are refreshed from token roles. Manual overrides can be added later.

## API and audit tables

### api_request_logs
Stores API request metadata: request ID, path, method, status code, duration, user, Keycloak identifiers, IP, user agent, and errors.

### audit_events
Stores important security and business events such as pipeline run triggered, user permission denied, feedback submitted, report downloaded, and role mapping changed.

## Pipeline and task tables

### pipeline_runs
Represents one full pipeline execution such as a full demo run or LLM reranker run. Tracks status, trigger user, started/completed timestamps, metrics, and error message.

### pipeline_run_steps
Represents individual steps inside a pipeline: backfill jobs, backfill resumes, build job tower, build candidate tower, index jobs, index candidates, baseline matching, LLM reranking, and comparison.

### pipeline_run_summaries
Stores summary payloads from pipeline stages, for example baseline summary, LLM rerank summary, and comparison summary.

### celery_queues
Defines known logical queues: `etl_queue`, `embedding_queue`, `matching_queue`, `llm_queue`, and `maintenance_queue`.

### celery_tasks
Tracks Celery task status, queue, payload, result, errors, retries, worker, and link to pipeline run.

### celery_task_events
Stores important task events and progress updates.

### processing_failures
Stores structured failures such as invalid job artifact, Qdrant upsert failure, Ollama timeout, invalid LLM JSON, or MongoDB write failure.

### file_artifacts
Registry for generated or uploaded files. For MongoDB-owned business data, it should use references instead of duplicating full data.

## Candidate portal tables

### candidate_user_links
Maps Keycloak app users to MongoDB `candidate_tower_records.candidate_id` and `resume_id`. This is how a logged-in candidate sees their own profile and recommendations.

### candidate_job_preferences
Future-ready table for candidate preferences such as preferred roles, locations, remote preference, salary expectation, and notice period.

### candidate_saved_jobs
Tracks saved, shortlisted, removed, or not-interested jobs for a candidate user.

### candidate_job_applications
Tracks candidate application lifecycle. MVP statuses include `selected`, `apply_clicked`, `applied_manually`, and `not_interested`. Future agent statuses are already supported: `agent_apply_requested`, `agent_draft_ready`, `user_approved_agent_apply`, `agent_applied`, and `agent_failed`.

### application_events
Append-only event stream for candidate job actions such as job saved, selected, apply button clicked, feedback added, and future agent application events.

### candidate_recommendation_views
Tracks which recommendations were viewed or clicked. Useful later for ranking analytics.

## No-duplication rule

Do not copy MongoDB collections into PostgreSQL. PostgreSQL stores control-plane data and user interaction data. MongoDB remains the source of truth for: `jobs_current`, `resume_profiles_current`, `job_tower_records`, `candidate_tower_records`, `candidate_job_matches`, `candidate_job_matches_llm_reranked`, `candidate_job_feedback`, and `candidate_job_training_pairs`.
