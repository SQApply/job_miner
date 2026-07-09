from __future__ import annotations


class EnvironmentVariables:
    POSTGRES_URL = "JOB_MINER_POSTGRES_URL"
    REDIS_URL = "JOB_MINER_REDIS_URL"
    API_CORS_ORIGINS = "JOB_MINER_API_CORS_ORIGINS"
    KEYCLOAK_ISSUER_URL = "JOB_MINER_KEYCLOAK_ISSUER_URL"
    KEYCLOAK_JWKS_URL = "JOB_MINER_KEYCLOAK_JWKS_URL"
    KEYCLOAK_AUDIENCE = "JOB_MINER_KEYCLOAK_AUDIENCE"
    KEYCLOAK_CLIENT_ID = "JOB_MINER_KEYCLOAK_CLIENT_ID"
    KEYCLOAK_REALM = "JOB_MINER_KEYCLOAK_REALM"
    KEYCLOAK_PUBLIC_BASE_URL = "JOB_MINER_KEYCLOAK_PUBLIC_BASE_URL"
    KEYCLOAK_ENABLED = "JOB_MINER_KEYCLOAK_ENABLED"
    DEFAULT_ORG_CODE = "JOB_MINER_DEFAULT_ORG_CODE"
    APP_ENV = "JOB_MINER_ENV"


class MongoCollections:
    JOBS_CURRENT = "jobs_current"
    RESUME_PROFILES_CURRENT = "resume_profiles_current"
    CANDIDATE_TOWER_RECORDS = "candidate_tower_records"
    JOB_TOWER_RECORDS = "job_tower_records"
    CANDIDATE_JOB_MATCHES = "candidate_job_matches"
    CANDIDATE_JOB_MATCHES_LLM_RERANKED = "candidate_job_matches_llm_reranked"
    CANDIDATE_JOB_FEEDBACK = "candidate_job_feedback"


class CandidateFields:
    CANDIDATE_ID = "candidate_id"
    RESUME_ID = "resume_id"
    FULL_NAME = "full_name"
    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    CURRENT_TITLE = "current_title"
    CURRENT_COMPANY = "current_company"
    SKILLS = "skills"
    PRIMARY_SKILLS = "primary_skills"  # legacy only
    SECONDARY_SKILLS = "secondary_skills"  # legacy only
    DOMAINS = "domains"
    CANDIDATE_EMBEDDING_TEXT = "candidate_embedding_text"


class ResumeFields:
    RESUME_ID = "resume_id"
    SHA256 = "sha256"
    SOURCE_FILE_NAME = "source_file_name"
    CONTACT = "contact"
    HEADLINE = "headline"
    SUMMARY = "summary"
    CURRENT_TITLE = "current_title"
    CURRENT_COMPANY = "current_company"
    TOTAL_EXPERIENCE_YEARS = "total_experience_years"
    PRIMARY_SKILLS = "primary_skills"
    SECONDARY_SKILLS = "secondary_skills"
    TOOLS_AND_PLATFORMS = "tools_and_platforms"
    PROGRAMMING_LANGUAGES = "programming_languages"
    DOMAINS = "domains"
    EXPERIENCE = "experience"
    EDUCATION = "education"


class JobFields:
    JOB_ID = "job_id"
    TITLE = "title"
    COMPANY = "company"
    LOCATION_TEXT = "location_text"
    JOB_URL = "job_url"
    APPLY_URL = "apply_url"
    SUMMARY = "summary"
    RESPONSIBILITIES = "responsibilities"
    REQUIRED_SKILLS = "required_skills"
    PREFERRED_SKILLS = "preferred_skills"


class MatchFields:
    MATCH_RUN_ID = "match_run_id"
    CANDIDATE_ID = "candidate_id"
    RESUME_ID = "resume_id"
    CANDIDATE_NAME = "candidate_name"
    JOB_ID = "job_id"
    RANK = "rank"
    FINAL_RANK = "final_rank"
    SCORE = "score"
    VECTOR_SCORE = "vector_score"
    BASELINE_SCORE = "baseline_score_0_1"
    LLM_SCORE = "llm_match_score_0_100"
    FINAL_SCORE = "final_score_0_100"
    LLM_DECISION = "llm_decision"
    LLM_REASON = "llm_reason"
    EVIDENCE = "evidence"


class ApplicationStatuses:
    SAVED = "saved"
    SELECTED = "selected"
    APPLY_CLICKED = "apply_clicked"
    APPLIED_MANUALLY = "applied_manually"
    NOT_INTERESTED = "not_interested"
    AGENT_APPLY_REQUESTED = "agent_apply_requested"
    AGENT_RUNNING = "agent_running"
    AGENT_DRAFT_READY = "agent_draft_ready"
    USER_APPROVED_AGENT_APPLY = "user_approved_agent_apply"
    AGENT_NEEDS_REVIEW = "agent_needs_review"
    AGENT_BLOCKED_LOGIN = "agent_blocked_login"
    AGENT_BLOCKED_CAPTCHA = "agent_blocked_captcha"
    AGENT_UNSUPPORTED_PORTAL = "agent_unsupported_portal"
    AGENT_SKIPPED_DUPLICATE = "agent_skipped_duplicate"
    AGENT_APPLIED = "agent_applied"
    AGENT_FAILED = "agent_failed"


class ApplicationBatchStatuses:
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApplicationRunStatuses:
    QUEUED = "queued"
    RUNNING = "running"
    PRECHECK_FAILED = "precheck_failed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED_EXTERNAL_LOGIN = "blocked_external_login"
    BLOCKED_CAPTCHA = "blocked_captcha"
    UNSUPPORTED_PORTAL = "unsupported_portal"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"


class UserRoles:
    PLATFORM_ADMIN = "platform_admin"
    TENANT_ADMIN = "tenant_admin"
    CANDIDATE = "candidate"
    RECRUITER = "recruiter"
    VIEWER = "viewer"
