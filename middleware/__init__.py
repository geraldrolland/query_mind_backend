"""Middleware: request logging, authentication (auth_token cookie) and Redis response cache."""

import hashlib
import json
import logging
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from app.auth.utils import decode_token, unsign_cookie
from app.core.settings import settings
from app.redis_conf import redis_client

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        logger.info(
            "REQ %s %s%s",
            request.method,
            request.url.path,
            f"?{request.url.query}" if request.url.query else "",
        )
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        user_id = getattr(getattr(request, "state", None), "auth_user", {}).get("id")
        logger.info(
            "RES %s %s -> %s (%dms, user=%s)",
            request.method,
            request.url.path,
            response.status_code,
            round(elapsed_ms),
            user_id,
        )
        return response


def _cors_headers(request: Request) -> dict:
    origin = request.headers.get("origin", "")
    if origin in settings.CORS_ORIGINS:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}

PUBLIC_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/google-auth",
    "/api/v1/auth/google/url",
    "/api/v1/auth/refresh-token",
    "/api/v1/auth/session",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
}

AUTH_PREFIX = "/api/v1/auth"


def is_public(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    # Public auth sub-routes: email, verify-email, reset-password, verify-otp, resend-otp
    if path.startswith(AUTH_PREFIX) and any(
        path.startswith(f"{AUTH_PREFIX}/{p}")
        for p in ("email", "verify-email", "reset-password", "verify-otp", "resend-otp")
    ):
        return True
    return False


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Let CORS preflight requests through to the CORSMiddleware.
        if request.method == "OPTIONS":
            return await call_next(request)

        if is_public(path):
            return await call_next(request)

        if not path.startswith("/api/v1") and path != "/health":
            return await call_next(request)

        raw = request.cookies.get("auth_token")
        if not raw:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"}, headers=_cors_headers(request))

        payload = unsign_cookie(raw, max_age=None)
        if not payload:
            return JSONResponse(status_code=401, content={"detail": "Invalid session"}, headers=_cors_headers(request))

        access = decode_token(payload.get("access_token"), "access_token")
        if access is None:
            return JSONResponse(status_code=401, content={"detail": "Access token expired"}, headers=_cors_headers(request))

        user_agent = request.headers.get("user-agent", "")
        if access.get("user_agent") and not _agent_matches(access.get("user_agent"), user_agent):
            return JSONResponse(status_code=401, content={"detail": "Session agent mismatch"}, headers=_cors_headers(request))

        request.state.auth_user = {
            "id": int(access["sub"]),
            "email": access.get("email"),
            "user_agent": user_agent,
        }
        return await call_next(request)


def _agent_matches(a: str, b: str) -> bool:
    import hashlib

    return hashlib.sha256(a.encode()).hexdigest() == hashlib.sha256(b.encode()).hexdigest() or a == b


CACHE_TTL_SECONDS = 5 * 60
_CACHE_PREFIX = "cache:v1"
SAFE_CACHE_PATHS = ("/api/v1/datasets",)


def _cache_key(user_id: int, method: str, path: str, query: str, body: bytes = b"") -> str:
    h = hashlib.sha256()
    h.update(method.encode())
    h.update(path.encode())
    h.update(b"?")
    h.update(query.encode())
    h.update(b"#")
    h.update(body)
    return f"{_CACHE_PREFIX}:{user_id}:{h.hexdigest()}"


def invalidate_user_cache(user_id: int) -> None:
    """Delete all cached GET responses for a user (after upload/delete)."""
    try:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(
                cursor=cursor, match=f"{_CACHE_PREFIX}:{user_id}:*", count=200
            )
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning("Cache invalidation failed: %s", exc)


class CacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in SAFE_CACHE_PATHS):
            return await call_next(request)
        if request.method != "GET":
            return await call_next(request)

        auth_user = getattr(request.state, "auth_user", None)
        user_id = auth_user.get("id") if auth_user else None
        if user_id is None:
            return await call_next(request)

        raw_body = await request.body()
        key = _cache_key(user_id, request.method, path, request.url.query, raw_body)
        try:
            cached = redis_client.get(key)
        except Exception as exc:
            logger.warning("Cache read failed: %s", exc)
            cached = None

        if cached is not None:
            try:
                payload = json.loads(cached)
                return JSONResponse(
                    content=payload["body"],
                    status_code=payload.get("status", 200),
                    media_type=payload.get("media_type", "application/json"),
                )
            except Exception as exc:
                logger.warning("Corrupt cache entry dropped: %s", exc)

        response = await call_next(request)

        if response.status_code != 200:
            return response

        media_type = (response.headers.get("content-type") or response.media_type or "").lower()
        if "json" not in media_type:
            return response

        try:
            body = b"".join([chunk async for chunk in response.body_iterator])
        except Exception as exc:
            logger.warning("Cache body read failed: %s", exc)
            return response
        if not body:
            return response

        rebuilt = Response(
            content=body,
            status_code=response.status_code,
            headers={
                k: v
                for k, v in response.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding")
            },
        )

        try:
            redis_client.set(
                key,
                json.dumps(
                    {
                        "status": response.status_code,
                        "media_type": response.media_type,
                        "body": json.loads(body.decode("utf-8")),
                    }
                ),
                ex=CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("Cache write failed: %s", exc)

        return rebuilt
