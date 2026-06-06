#!/bin/sh
set -eu

REALM="${JOB_MINER_KEYCLOAK_REALM:-job-miner}"
KC_SERVER="${JOB_MINER_KEYCLOAK_PUBLIC_BASE_URL:-http://localhost:8080}"
ADMIN_USER="${KEYCLOAK_ADMIN:-admin}"
ADMIN_PASSWORD="${KEYCLOAK_ADMIN_PASSWORD:-admin}"
GOOGLE_ID="${GOOGLE_CLIENT_ID:-}"
GOOGLE_SECRET="${GOOGLE_CLIENT_SECRET:-}"
WEB_CLIENT_ID="${JOB_MINER_KEYCLOAK_CLIENT_ID:-job-miner-web}"
API_CLIENT_ID="${JOB_MINER_KEYCLOAK_AUDIENCE:-job-miner-api}"
AUDIENCE_MAPPER_NAME="${JOB_MINER_KEYCLOAK_AUDIENCE_MAPPER_NAME:-job-miner-api-audience}"
DEFAULT_ROLE="${JOB_MINER_DEFAULT_USER_ROLE:-candidate}"
VERIFY_EMAIL="${JOB_MINER_KEYCLOAK_VERIFY_EMAIL:-false}"
SMTP_HOST="${JOB_MINER_KEYCLOAK_SMTP_HOST:-}"
SMTP_PORT="${JOB_MINER_KEYCLOAK_SMTP_PORT:-587}"
SMTP_FROM="${JOB_MINER_KEYCLOAK_SMTP_FROM:-}"
SMTP_FROM_DISPLAY_NAME="${JOB_MINER_KEYCLOAK_SMTP_FROM_DISPLAY_NAME:-Job Miner}"
SMTP_REPLY_TO="${JOB_MINER_KEYCLOAK_SMTP_REPLY_TO:-}"
SMTP_AUTH="${JOB_MINER_KEYCLOAK_SMTP_AUTH:-true}"
SMTP_USER="${JOB_MINER_KEYCLOAK_SMTP_USER:-}"
SMTP_PASSWORD="${JOB_MINER_KEYCLOAK_SMTP_PASSWORD:-}"
SMTP_STARTTLS="${JOB_MINER_KEYCLOAK_SMTP_STARTTLS:-true}"
SMTP_SSL="${JOB_MINER_KEYCLOAK_SMTP_SSL:-false}"

case "$(printf '%s' "$VERIFY_EMAIL" | tr '[:upper:]' '[:lower:]')" in
  1|true|yes) VERIFY_EMAIL="true" ;;
  *) VERIFY_EMAIL="false" ;;
esac

kc() {
  /opt/keycloak/bin/kcadm.sh "$@"
}

get_client_uuid() {
  client_id="$1"
  kc get clients -r "$REALM" -q clientId="$client_id" --fields id --format csv 2>/dev/null \
    | tail -n 1 \
    | tr -d '"\r'
}

# get_protocol_mapper_uuid() {
#   client_uuid="$1"
#   mapper_name="$2"
#   kc get "clients/$client_uuid/protocol-mappers/models" -r "$REALM" --fields id,name --format csv 2>/dev/null \
#     | awk -F, -v mapper="$mapper_name" '
#         NR > 1 {
#           gsub(/"/, "", $1);
#           gsub(/"/, "", $2);
#           gsub(/\r/, "", $1);
#           gsub(/\r/, "", $2);
#           if ($2 == mapper) { print $1; exit }
#         }
#       '
# }

get_protocol_mapper_uuid() {
  client_uuid="$1"
  mapper_name="$2"

  kc get "clients/$client_uuid/protocol-mappers/models" -r "$REALM" --fields id,name --format csv 2>/dev/null \
    | tr -d '\r"' \
    | while IFS=, read -r mapper_id mapper_label; do
        if [ "$mapper_label" = "$mapper_name" ]; then
          printf '%s\n' "$mapper_id"
          break
        fi
      done
}
if [ -z "$GOOGLE_ID" ] || [ -z "$GOOGLE_SECRET" ] || [ "$GOOGLE_ID" = "change-me" ] || [ "$GOOGLE_SECRET" = "change-me" ]; then
  echo "[WARN] GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are missing or still set to change-me."
  echo "[WARN] The Google button can be configured only after real credentials are added to .env."
fi

echo "[INFO] Logging in to Keycloak admin API at $KC_SERVER"
kc config credentials \
  --server "$KC_SERVER" \
  --realm master \
  --user "$ADMIN_USER" \
  --password "$ADMIN_PASSWORD"

if [ "$VERIFY_EMAIL" = "true" ] && [ -z "$SMTP_HOST" ]; then
  echo "[WARN] JOB_MINER_KEYCLOAK_VERIFY_EMAIL=true but SMTP is not configured."
  echo "[WARN] Disabling verifyEmail for local/dev so signup is not blocked. Configure SMTP before enabling this in production."
  VERIFY_EMAIL="false"
fi

if [ "$VERIFY_EMAIL" = "false" ]; then
  echo "[WARN] Keycloak email verification is DISABLED. This is acceptable only for local/dev testing."
fi

echo "[INFO] Applying Job Miner realm login and security settings"
kc update "realms/$REALM" \
  -s enabled=true \
  -s loginTheme=job-miner \
  -s registrationAllowed=true \
  -s loginWithEmailAllowed=true \
  -s registrationEmailAsUsername=true \
  -s duplicateEmailsAllowed=false \
  -s resetPasswordAllowed=true \
  -s verifyEmail="$VERIFY_EMAIL" \
  -s passwordPolicy="length(8) and upperCase(1) and specialChars(1)" \
  -s rememberMe=true \
  -s bruteForceProtected=true

if [ -n "$SMTP_HOST" ]; then
  echo "[INFO] Applying Keycloak SMTP settings from environment."
  kc update "realms/$REALM" \
    -s "smtpServer.host=$SMTP_HOST" \
    -s "smtpServer.port=$SMTP_PORT" \
    -s "smtpServer.from=$SMTP_FROM" \
    -s "smtpServer.fromDisplayName=$SMTP_FROM_DISPLAY_NAME" \
    -s "smtpServer.replyTo=$SMTP_REPLY_TO" \
    -s "smtpServer.auth=$SMTP_AUTH" \
    -s "smtpServer.user=$SMTP_USER" \
    -s "smtpServer.password=$SMTP_PASSWORD" \
    -s "smtpServer.starttls=$SMTP_STARTTLS" \
    -s "smtpServer.ssl=$SMTP_SSL"
else
  echo "[INFO] SMTP is not configured. Email verification remains disabled for local/dev."
fi

if ! kc get "roles/$DEFAULT_ROLE" -r "$REALM" >/dev/null 2>&1; then
  echo "[INFO] Creating missing realm role: $DEFAULT_ROLE"
  kc create roles -r "$REALM" -s name="$DEFAULT_ROLE" -s description="Default candidate user role"
fi

# New self-registered and Google-brokered users must become candidates by default.
if kc get "roles/default-roles-$REALM/composites" -r "$REALM" 2>/dev/null | grep -q '"name"[[:space:]]*:[[:space:]]*"'"$DEFAULT_ROLE"'"'; then
  echo "[INFO] Default realm role already includes '$DEFAULT_ROLE'"
else
  echo "[INFO] Adding '$DEFAULT_ROLE' to default realm roles"
  kc add-roles -r "$REALM" --rname "default-roles-$REALM" --rolename "$DEFAULT_ROLE" || \
    echo "[WARN] Could not add '$DEFAULT_ROLE' to default realm roles. Check role configuration manually."
fi

API_CLIENT_UUID="$(get_client_uuid "$API_CLIENT_ID")"
if [ -z "$API_CLIENT_UUID" ]; then
  echo "[INFO] Creating missing API audience client: $API_CLIENT_ID"
  kc create clients -r "$REALM" \
    -s clientId="$API_CLIENT_ID" \
    -s enabled=true \
    -s protocol=openid-connect \
    -s publicClient=false \
    -s bearerOnly=true \
    -s standardFlowEnabled=false \
    -s directAccessGrantsEnabled=false \
    -s serviceAccountsEnabled=false >/dev/null
else
  echo "[INFO] API audience client exists: $API_CLIENT_ID"
fi

WEB_CLIENT_UUID="$(get_client_uuid "$WEB_CLIENT_ID")"
if [ -z "$WEB_CLIENT_UUID" ]; then
  echo "[ERROR] Frontend client '$WEB_CLIENT_ID' does not exist in realm '$REALM'."
  exit 1
fi

# MAPPER_UUID="$(get_protocol_mapper_uuid "$WEB_CLIENT_UUID" "$AUDIENCE_MAPPER_NAME")"
# if [ -z "$MAPPER_UUID" ]; then
#   echo "[INFO] Creating access-token audience mapper '$AUDIENCE_MAPPER_NAME' on client '$WEB_CLIENT_ID'"
#   kc create "clients/$WEB_CLIENT_UUID/protocol-mappers/models" -r "$REALM" \
#     -s name="$AUDIENCE_MAPPER_NAME" \
#     -s protocol=openid-connect \
#     -s protocolMapper=oidc-audience-mapper \
#     -s consentRequired=false \
#     -s config."included.client.audience"="$API_CLIENT_ID" \
#     -s config."id.token.claim"="false" \
#     -s config."access.token.claim"="true" >/dev/null
# else
#   echo "[INFO] Updating access-token audience mapper '$AUDIENCE_MAPPER_NAME' on client '$WEB_CLIENT_ID'"
#   kc update "clients/$WEB_CLIENT_UUID/protocol-mappers/models/$MAPPER_UUID" -r "$REALM" \
#     -s name="$AUDIENCE_MAPPER_NAME" \
#     -s protocol=openid-connect \
#     -s protocolMapper=oidc-audience-mapper \
#     -s consentRequired=false \
#     -s config."included.client.audience"="$API_CLIENT_ID" \
#     -s config."id.token.claim"="false" \
#     -s config."access.token.claim"="true"
# fi

MAPPER_UUID="$(get_protocol_mapper_uuid "$WEB_CLIENT_UUID" "$AUDIENCE_MAPPER_NAME")"
if [ -z "$MAPPER_UUID" ]; then
  echo "[INFO] Creating access-token audience mapper '$AUDIENCE_MAPPER_NAME' on client '$WEB_CLIENT_ID'"
  kc create "clients/$WEB_CLIENT_UUID/protocol-mappers/models" -r "$REALM" \
    -s "name=$AUDIENCE_MAPPER_NAME" \
    -s "protocol=openid-connect" \
    -s "protocolMapper=oidc-audience-mapper" \
    -s "consentRequired=false" \
    -s "config.\"included.client.audience\"=$API_CLIENT_ID" \
    -s "config.\"id.token.claim\"=false" \
    -s "config.\"access.token.claim\"=true" >/dev/null
else
  echo "[INFO] Updating access-token audience mapper '$AUDIENCE_MAPPER_NAME' on client '$WEB_CLIENT_ID'"
  kc update "clients/$WEB_CLIENT_UUID/protocol-mappers/models/$MAPPER_UUID" -r "$REALM" \
    -s "name=$AUDIENCE_MAPPER_NAME" \
    -s "protocol=openid-connect" \
    -s "protocolMapper=oidc-audience-mapper" \
    -s "consentRequired=false" \
    -s "config.\"included.client.audience\"=$API_CLIENT_ID" \
    -s "config.\"id.token.claim\"=false" \
    -s "config.\"access.token.claim\"=true"
fi

if kc get "identity-provider/instances/google" -r "$REALM" >/dev/null 2>&1; then
  echo "[INFO] Updating existing Google identity provider"
  kc update "identity-provider/instances/google" -r "$REALM" \
    -s alias=google \
    -s displayName=Google \
    -s providerId=google \
    -s enabled=true \
    -s trustEmail=true \
    -s storeToken=false \
    -s addReadTokenRoleOnCreate=false \
    -s authenticateByDefault=false \
    -s linkOnly=false \
    -s firstBrokerLoginFlowAlias="first broker login" \
    -s config.clientId="$GOOGLE_ID" \
    -s config.clientSecret="$GOOGLE_SECRET" \
    -s config.defaultScope="openid profile email" \
    -s config.syncMode=FORCE \
    -s config.useJwksUrl=true
else
  echo "[INFO] Creating Google identity provider"
  kc create "identity-provider/instances" -r "$REALM" \
    -s alias=google \
    -s displayName=Google \
    -s providerId=google \
    -s enabled=true \
    -s trustEmail=true \
    -s storeToken=false \
    -s addReadTokenRoleOnCreate=false \
    -s authenticateByDefault=false \
    -s linkOnly=false \
    -s firstBrokerLoginFlowAlias="first broker login" \
    -s config.clientId="$GOOGLE_ID" \
    -s config.clientSecret="$GOOGLE_SECRET" \
    -s config.defaultScope="openid profile email" \
    -s config.syncMode=FORCE \
    -s config.useJwksUrl=true
fi

echo "[INFO] Verifying access-token audience mapper"
MAPPER_UUID="$(get_protocol_mapper_uuid "$WEB_CLIENT_UUID" "$AUDIENCE_MAPPER_NAME")"
if [ -z "$MAPPER_UUID" ]; then
  echo "[ERROR] Audience mapper '$AUDIENCE_MAPPER_NAME' was not created. Backend strict JWT validation will reject tokens."
  exit 1
fi

echo "[SUCCESS] Realm '$REALM' is configured: theme=job-miner, google IdP enabled, access-token audience=$API_CLIENT_ID, default role=$DEFAULT_ROLE."
