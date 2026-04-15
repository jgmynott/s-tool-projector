"""
Clerk JWT verification.

FastAPI dependency that verifies the `Authorization: Bearer <token>` header
against Clerk's JWKS (RS256). Returns the decoded claims dict on success,
or None when no token is present (caller decides whether that's OK).

JWKS is cached in-process for 1 hour. A background refresh on key-not-found
handles key rotation without restarting the app.

If CLERK_JWKS_URL is unset, verification is disabled and every request is
treated as anonymous — useful for local dev before Clerk is configured.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import httpx
import jwt
from fastapi import Header, HTTPException

log = logging.getLogger("auth")

CLERK_JWKS_URL = os.getenv("CLERK_JWKS_URL", "").strip()
_JWKS_TTL_SECONDS = 3600

_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float = 0
_jwks_lock = threading.Lock()


def _fetch_jwks(force: bool = False) -> dict[str, Any]:
    global _jwks_cache, _jwks_fetched_at
    with _jwks_lock:
        age = time.time() - _jwks_fetched_at
        if not force and _jwks_cache and age < _JWKS_TTL_SECONDS:
            return _jwks_cache
        if not CLERK_JWKS_URL:
            return {}
        try:
            r = httpx.get(CLERK_JWKS_URL, timeout=5.0)
            r.raise_for_status()
            _jwks_cache = r.json()
            _jwks_fetched_at = time.time()
            log.info("Fetched Clerk JWKS: %d keys", len(_jwks_cache.get("keys", [])))
        except Exception as e:
            log.warning("JWKS fetch failed: %s", e)
        return _jwks_cache


def _key_for_kid(kid: str) -> Optional[dict]:
    jwks = _fetch_jwks()
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            return k
    # kid not found — refresh once in case keys rotated
    jwks = _fetch_jwks(force=True)
    for k in jwks.get("keys", []):
        if k.get("kid") == kid:
            return k
    return None


def verify_token(token: str) -> dict[str, Any]:
    """Verify a Clerk-issued JWT and return the claims. Raises on failure."""
    if not CLERK_JWKS_URL:
        raise HTTPException(status_code=503, detail="Auth not configured")
    try:
        unverified = jwt.get_unverified_header(token)
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Malformed token: {e}")
    kid = unverified.get("kid")
    if not kid:
        raise HTTPException(status_code=401, detail="Token missing kid")
    key = _key_for_kid(kid)
    if not key:
        raise HTTPException(status_code=401, detail="Unknown signing key")
    public_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
    try:
        # Clerk sets `azp` (authorized party) rather than a fixed audience, so
        # we skip audience verification and rely on signature + expiry.
        claims = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.PyJWTError as e:
        raise HTTPException(status_code=401, detail=f"Token invalid: {e}")
    return claims


# ── FastAPI dependencies ────────────────────────────────────────────────

def current_user(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    """Required auth. Raises 401 if no valid bearer token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    claims = verify_token(token)
    return {
        "user_id": claims.get("sub"),
        "email": claims.get("email"),
        "claims": claims,
    }


def optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict[str, Any]]:
    """Optional auth. Returns None for anonymous requests, user dict if authed."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    try:
        claims = verify_token(token)
    except HTTPException:
        return None
    return {
        "user_id": claims.get("sub"),
        "email": claims.get("email"),
        "claims": claims,
    }
