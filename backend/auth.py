"""Authentication and authorisation.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with per-user salts, never
in plaintext. A successful login returns a signed, expiring bearer token; every
protected route verifies that token's signature and expiry before reading the
role from it. The client never asserts its own role.

Deliberately built on the standard library only — hashlib, hmac, secrets — so
there is no additional dependency to install on the deployment host.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from fastapi import Header, HTTPException, status

# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

PBKDF2_ROUNDS = 200_000
TOKEN_TTL_SECONDS = 8 * 60 * 60          # a working day
WS_TICKET_TTL_SECONDS = 60               # single-use window for a socket upgrade

# A missing secret means tokens are signed with a value that changes on every
# restart, which invalidates old sessions rather than trusting a known default.
SECRET_KEY = os.environ.get("RM_SECRET_KEY") or secrets.token_hex(32)

ROLES = ("admin", "student")


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return digest.hex(), salt


def _build_users() -> dict[str, dict[str, Any]]:
    """Credentials come from the environment; the demo defaults are a fallback.

    Set RM_ADMIN_PASSWORD and RM_STUDENT_PASSWORD on a real deployment.
    """
    accounts = {
        "admin": (os.environ.get("RM_ADMIN_PASSWORD", "admin"), "admin", "Placement Coordinator"),
        "student": (os.environ.get("RM_STUDENT_PASSWORD", "student"), "student", "Student"),
    }
    users = {}
    for username, (password, role, label) in accounts.items():
        digest, salt = hash_password(password)
        users[username] = {"hash": digest, "salt": salt, "role": role, "label": label}
    return users


USERS = _build_users()


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(SECRET_KEY.encode(), payload, hashlib.sha256).digest())


def issue_token(username: str, role: str, ttl: int = TOKEN_TTL_SECONDS,
                purpose: str = "session") -> str:
    body = json.dumps(
        {"sub": username, "role": role, "purpose": purpose,
         "exp": int(time.time()) + ttl, "jti": secrets.token_hex(8)},
        separators=(",", ":"), sort_keys=True,
    ).encode()
    return f"{_b64(body)}.{_sign(body)}"


def verify_token(token: str, purpose: str = "session") -> dict[str, Any]:
    """Return the token's claims, or raise 401.

    Signature is checked with a constant-time comparison so that a wrong token
    cannot be discovered a byte at a time by timing the response.
    """
    invalid = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Your session is invalid or has expired. Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        encoded_body, signature = token.split(".", 1)
        body = _unb64(encoded_body)
    except (ValueError, TypeError):
        raise invalid from None

    if not hmac.compare_digest(signature, _sign(body)):
        raise invalid

    try:
        claims = json.loads(body)
    except json.JSONDecodeError:
        raise invalid from None

    if claims.get("purpose") != purpose:
        raise invalid
    if claims.get("exp", 0) < time.time():
        raise invalid
    if claims.get("role") not in ROLES:
        raise invalid
    return claims


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """Verify credentials in constant time whether or not the user exists."""
    user = USERS.get(username.strip().lower())
    # Hash against a dummy salt for unknown users so that a missing account and
    # a wrong password take the same amount of time.
    salt = user["salt"] if user else "0" * 32
    expected = user["hash"] if user else "0" * 64
    candidate, _ = hash_password(password, salt)
    if not hmac.compare_digest(candidate, expected) or user is None:
        return None
    return user


# --------------------------------------------------------------------------
# dependencies
# --------------------------------------------------------------------------

def _token_from_header(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    return verify_token(_token_from_header(authorization))


def require_admin(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    claims = current_user(authorization)
    if claims["role"] != "admin":
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "This action is restricted to the placement coordinator.",
        )
    return claims
