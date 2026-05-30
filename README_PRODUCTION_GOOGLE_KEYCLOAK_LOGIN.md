# Production Google/Gmail Login Through Keycloak

This patch keeps Keycloak as the only identity authority for Job Miner. Google is configured as a Keycloak Identity Provider, so the browser receives a Keycloak access token and the FastAPI backend validates only Keycloak-issued tokens.

## What changed

1. **Strict JWT validation**
   - `verify_aud=False` has been removed.
   - The backend now requires issuer, signature, expiry, issued-at, `aud=job-miner-api`, and `azp=job-miner-web`.
   - Configure the Keycloak `job-miner-web` client with the included audience mapper.

2. **Google/Gmail login via Keycloak**
   - The realm import now includes a Google Identity Provider.
   - Set these environment variables before importing the realm:
     - `GOOGLE_CLIENT_ID`
     - `GOOGLE_CLIENT_SECRET`

3. **Duplicate email protection**
   - A new `job_miner_control.app_user_identities` table allows one app user to have multiple login identities.
   - If Google creates a different Keycloak subject for the same verified email, the backend attaches the new identity to the existing `app_users` row instead of crashing on duplicate email.

4. **Automatic candidate profile linking**
   - `/me` now provisions the candidate link automatically.
   - If exactly one MongoDB candidate profile matches the verified email, it links it.
   - If no profile exists, it creates an incomplete candidate shell and links it.
   - If multiple profiles match, it returns `profile_state=conflict` and does not expose candidate data.

5. **Manual self-linking disabled**
   - `/me/link-candidate` now returns `410 Gone`.
   - Admin repair is available at `/admin/users/{app_user_id}/link-candidate`.

6. **Naukri-style Keycloak theme**
   - Added a custom Keycloak login/register theme under `infra/keycloak/themes/job-miner`.
   - The realm uses `loginTheme=job-miner`.

## Required backend environment

```env
JOB_MINER_KEYCLOAK_ENABLED=true
JOB_MINER_KEYCLOAK_PUBLIC_BASE_URL=http://localhost:8080
JOB_MINER_KEYCLOAK_REALM=job-miner
JOB_MINER_KEYCLOAK_ISSUER_URL=http://localhost:8080/realms/job-miner
JOB_MINER_KEYCLOAK_JWKS_URL=http://localhost:8080/realms/job-miner/protocol/openid-connect/certs
JOB_MINER_KEYCLOAK_AUDIENCE=job-miner-api
JOB_MINER_KEYCLOAK_CLIENT_ID=job-miner-web
JOB_MINER_API_CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173
```

Do not set `JOB_MINER_ALLOW_DEV_AUTH_FALLBACK=true` outside local throwaway testing.

## Existing Postgres database migration

For a fresh Docker volume, `infra/postgres/init/001_job_miner_control.sql` is enough.

For an existing database, run:

```bash
docker exec -i job_miner_postgres psql -U job_miner_app -d job_miner_control < infra/postgres/migrations/002_auth_production_hardening.sql
```

If the migration fails while creating `uq_app_user_org_normalized_email`, you already have duplicate verified emails in the same organization. Resolve those duplicates before enabling Google login.

## Google Cloud Console setup

Create an OAuth 2.0 Client ID in Google Cloud Console and configure:

```text
Authorized redirect URI:
http://localhost:8080/realms/job-miner/broker/google/endpoint
```

For production, use your real Keycloak host:

```text
https://auth.yourdomain.com/realms/job-miner/broker/google/endpoint
```

Then set:

```bash
export GOOGLE_CLIENT_ID="..."
export GOOGLE_CLIENT_SECRET="..."
```

## Expected `/me` response states

```json
{
  "profile_state": "ready | incomplete | conflict | blocked | admin | not_candidate",
  "next_action": "show_profile | complete_profile | contact_support | verify_email | show_admin",
  "candidate_link": {}
}
```

Frontend routing now uses this state instead of asking a candidate to manually enter `candidate_id`.
