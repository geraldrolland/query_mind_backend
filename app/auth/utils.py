"""JWT generation/verification, password hashing, and cookie/CSRF helpers."""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from itsdangerous import BadSignature, TimestampSigner
from pwdlib import PasswordHash

from app.core.settings import settings

logger = logging.getLogger(__name__)

password_hash = PasswordHash.recommended()
cookie_signer = TimestampSigner(settings.SECRET_KEY)


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return password_hash.verify(password, hashed)
    except Exception:
        return False


def generate_access_token(payload: dict) -> str:
    data = {
        **payload,
        "type": "access_token",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def generate_refresh_token(payload: dict) -> str:
    data = {
        **payload,
        "type": "refresh_token",
        "jti": secrets.token_hex(16),
        "exp": datetime.now(timezone.utc) + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(data, settings.SECRET_KEY, algorithm="HS256")


def decode_token(token: str, expected_type: str = "access_token") -> dict | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if payload.get("type") != expected_type:
            return None
        return payload
    except jwt.PyJWTError:
        return None


def sign_cookie(value: dict) -> str:
    """Sign a JSON dict into an opaque cookie value."""
    raw = json.dumps(value, separators=(",", ":")).encode()
    return cookie_signer.sign(raw).decode()


def unsign_cookie(value: str, max_age: int | None = None) -> dict | None:
    try:
        raw = cookie_signer.unsign(value, max_age=max_age)
        return json.loads(raw.decode())
    except (BadSignature, json.JSONDecodeError, TypeError):
        return None


def gen_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def sign_csrf(token: str) -> str:
    return cookie_signer.sign(token.encode()).decode()


def verify_csrf(signed: str, provided: str, max_age: int | None = 3600) -> bool:
    """Verify a CSRF token: signed value matches and equals header value."""
    if not signed or not provided:
        return False
    try:
        raw = cookie_signer.unsign(signed, max_age=max_age).decode()
    except BadSignature:
        return False
    return hmac.compare_digest(raw, provided)


def make_tokens(user_id: int, email: str, user_agent: str) -> dict:
    """Create access+refresh token pair for a user."""
    access = generate_access_token({"sub": str(user_id), "email": email, "user_agent": user_agent})
    refresh = generate_refresh_token({"sub": str(user_id), "email": email, "user_agent": user_agent})
    jti = decode_token(refresh, "refresh_token").get("jti")
    return {"access": access, "refresh": refresh, "jti": jti}
