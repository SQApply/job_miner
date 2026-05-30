from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from ..common.env import load_runtime_env
from ..common.constants import EnvironmentVariables

load_runtime_env()
from ..control.postgres import postgres_session
from ..control.repository import ControlRepository, extract_roles

bearer_scheme = HTTPBearer(auto_error=False)


class KeycloakSettings:
    """Runtime settings for strict Keycloak JWT validation.

    The backend trusts only Keycloak-issued access tokens. Google/Gmail tokens are
    never accepted directly by the API; Google must be configured as a Keycloak
    identity provider so the browser still receives a Keycloak access token.
    """

    def __init__(self) -> None:
        self.enabled = os.getenv(EnvironmentVariables.KEYCLOAK_ENABLED, "true").lower() in {"1", "true", "yes"}
        self.allow_dev_auth_fallback = os.getenv("JOB_MINER_ALLOW_DEV_AUTH_FALLBACK", "false").lower() in {"1", "true", "yes"}

        public_base = os.getenv(EnvironmentVariables.KEYCLOAK_PUBLIC_BASE_URL, "http://localhost:8080")
        realm = os.getenv(EnvironmentVariables.KEYCLOAK_REALM, "job-miner")
        self.realm = realm
        self.public_base_url = public_base.rstrip("/")
        self.issuer_url = os.getenv(
            EnvironmentVariables.KEYCLOAK_ISSUER_URL,
            f"{self.public_base_url}/realms/{realm}",
        ).rstrip("/")
        self.jwks_url = os.getenv(
            EnvironmentVariables.KEYCLOAK_JWKS_URL,
            f"{self.issuer_url}/protocol/openid-connect/certs",
        )

        # Production contract: Keycloak must include this API audience in access tokens.
        # Configure an OIDC audience mapper on the job-miner-web client for job-miner-api.
        self.audience = os.getenv(EnvironmentVariables.KEYCLOAK_AUDIENCE, "job-miner-api")
        self.client_id = os.getenv(EnvironmentVariables.KEYCLOAK_CLIENT_ID, "job-miner-web")
        self.leeway_seconds = int(os.getenv("JOB_MINER_KEYCLOAK_JWT_LEEWAY_SECONDS", "30"))


@lru_cache(maxsize=1)
def get_keycloak_settings() -> KeycloakSettings:
    return KeycloakSettings()


@lru_cache(maxsize=1)
def get_jwk_client() -> PyJWKClient:
    return PyJWKClient(get_keycloak_settings().jwks_url)


def keycloak_public_config() -> dict[str, str]:
    settings = get_keycloak_settings()
    return {
        "enabled": str(settings.enabled).lower(),
        "url": settings.public_base_url,
        "realm": settings.realm,
        "clientId": settings.client_id,
    }


def _validate_access_token_contract(claims: dict[str, Any], settings: KeycloakSettings) -> None:
    token_type = str(claims.get("typ") or "").lower()
    if token_type and token_type != "bearer":
        raise ValueError("Token is not a Keycloak access token.")

    authorized_party = claims.get("azp")
    if authorized_party != settings.client_id:
        raise ValueError("Token authorized party does not match this frontend client.")

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        raise ValueError("Token subject is missing.")


def decode_keycloak_token(token: str) -> dict[str, Any]:
    settings = get_keycloak_settings()
    try:
        signing_key = get_jwk_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=settings.issuer_url,
            audience=settings.audience,
            leeway=settings.leeway_seconds,
            options={
                "require": ["exp", "iat", "iss", "sub", "aud"],
                "verify_signature": True,
                "verify_exp": True,
                "verify_iat": True,
                "verify_iss": True,
                "verify_aud": True,
            },
        )
        _validate_access_token_contract(claims, settings)
        return claims
    except Exception as exc:
        # Do not leak signing, issuer, audience, or token parsing details to callers.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme)) -> dict[str, Any]:
    settings = get_keycloak_settings()
    if not settings.enabled:
        if not settings.allow_dev_auth_fallback:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Keycloak authentication is disabled and development auth fallback is not allowed.",
            )
        return {
            "id": None,
            "organization_id": None,
            "email": "dev@jobminer.local",
            "email_verified": True,
            "full_name": "Dev User",
            "keycloak_user_id": "dev-user",
            "login_keycloak_user_id": "dev-user",
            "roles": ["platform_admin", "candidate"],
            "claims": {},
        }

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_keycloak_token(credentials.credentials)
    with postgres_session() as session:
        repo = ControlRepository(session)
        try:
            user = repo.upsert_user_from_token(claims)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        roles = extract_roles(claims)
        user["roles"] = roles
        user["claims"] = claims
        return user


def require_permission(permission_key: str):
    def dependency(user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
        if "platform_admin" in user.get("roles", []):
            return user
        with postgres_session() as session:
            repo = ControlRepository(session)
            if user.get("id") and repo.user_has_permission(str(user["id"]), permission_key):
                return user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Missing permission: {permission_key}")

    return dependency
