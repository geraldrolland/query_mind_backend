"""Auth routes: register, login, me, refresh, logout, verify, reset, Google OAuth."""

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.auth.oauth import build_google_auth_url, exchange_code_for_user
from app.auth.utils import (
    decode_token,
    gen_csrf_token,
    hash_password,
    make_tokens,
    sign_cookie,
    sign_csrf,
    unsign_cookie,
    verify_password,
)
from app.core.settings import settings
from app.db import get_session
from app.email.sender import render_reset_email, render_verification_email, send_email
from app.models.user import User
from app.redis_conf import refresh_redis, redis_client
from app.schemas.auth import (
    EmailSchema,
    ResetPasswordSchema,
    SessionExchangeSchema,
    UserLoginSchema,
    UserOut,
    UserRegistrationSchema,
    VerifyEmailSchema,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _set_session_cookies(response: Response, user: User, user_agent: str) -> Response:
    tokens = make_tokens(user.id, user.email, user_agent)
    # Store refresh token jti in Redis for rotation/revocation.
    refresh_redis.set(
        f"refresh:{tokens['jti']}",
        str(user.id),
        ex=settings.JWT_REFRESH_EXPIRE_DAYS * 86400,
    )
    signed = sign_cookie(
        {"access_token": tokens["access"], "refresh_token": tokens["refresh"]}
    )
    csrf = sign_csrf(gen_csrf_token())
    response.set_cookie(
        key="auth_token",
        value=signed,
        httponly=settings.COOKIE_HTTPONLY,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
        max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 86400,
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf,
        httponly=settings.COOKIE_HTTPONLY,
        secure=settings.COOKIE_SECURE,
        samesite=settings.COOKIE_SAMESITE,
    )

    logger.info("Set session cookies for user %s", user.email)

    return response


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(
    data: UserRegistrationSchema,
    response: Response,
    session: Session = Depends(get_session),
):
    existing = session.exec(select(User).where(User.email == data.email)).first()
    if existing:
        raise HTTPException(status_code=400, detail="user with this email already exists")

    user = User(
        email=data.email,
        hashed_password=hash_password(data.password),
        auth_provider="email",
        is_email_verified=False,
    )
    session.add(user)
    session.commit()
    session.refresh(user)

    _send_verification_link(user.email, session)

    response.status_code = status.HTTP_201_CREATED
    return {"detail": "user registered successfully", "verification_required": True}


def _send_verification_link(email: str, session: Session) -> None:
    token = secrets.token_urlsafe(32)
    redis_client.set(f"verify:{token}", email, ex=30 * 60)
    link = f"{settings.APP_HOST}/verify-email?token={token}&email={email}"
    sent = send_email(
        email,
        "Verify your QueryMind email",
        render_verification_email(link),
    )
    if not sent:
        # Dev-only delivery: log the link so the flow is usable without SMTP.
        logger.info("EMAIL_VERIFICATION_LINK: %s", link)


@router.post("/login", status_code=status.HTTP_200_OK)
async def login_user(
    data: UserLoginSchema,
    response: Response,
    request: Request,
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="invalid email or password")
    if not user.is_email_verified:
        _send_verification_link(user.email, session)
        raise HTTPException(
            status_code=403,
            detail="email not verified, verification link sent",
        )

    _set_session_cookies(response, user, request.headers.get("user-agent", ""))
    return {"detail": "user logged in successfully"}


@router.get("/me", status_code=status.HTTP_200_OK)
async def get_current_user(request: Request):
    user = request.state.auth_user
    return {"id": user["id"], "email": user["email"], "auth_provider": "email"}


@router.post("/refresh-token", status_code=status.HTTP_200_OK)
async def refresh_token(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    raw = request.cookies.get("auth_token")
    if not raw:
        raise HTTPException(status_code=401, detail="not authenticated")
    payload = unsign_cookie(raw, max_age=None)
    if not payload:
        raise HTTPException(status_code=401, detail="invalid session")

    refresh = decode_token(payload.get("refresh_token"), "refresh_token")
    if refresh is None:
        raise HTTPException(status_code=401, detail="refresh token expired")

    jti = refresh.get("jti")
    if not jti or not refresh_redis.get(f"refresh:{jti}"):
        raise HTTPException(status_code=401, detail="refresh token revoked")

    user = session.get(User, int(refresh["sub"]))
    if not user:
        raise HTTPException(status_code=401, detail="user not found")

    # Rotate: revoke old jti, issue fresh pair.
    refresh_redis.delete(f"refresh:{jti}")
    _set_session_cookies(response, user, request.headers.get("user-agent", ""))
    return {"detail": "tokens refreshed"}


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout_user(request: Request, response: Response):
    raw = request.cookies.get("auth_token")
    if raw:
        payload = unsign_cookie(raw, max_age=None)
        if payload:
            refresh = decode_token(payload.get("refresh_token"), "refresh_token")
            if refresh and refresh.get("jti"):
                refresh_redis.delete(f"refresh:{refresh['jti']}")
    response.delete_cookie("auth_token")
    response.delete_cookie("csrf_token")
    return {"detail": "logged out"}


# --- Email verification & password reset ---

@router.post("/email", status_code=status.HTTP_200_OK)
async def request_verification_email(
    data: EmailSchema, session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.is_email_verified:
        return {"detail": "email already verified"}
    _send_verification_link(user.email, session)
    return {"detail": "verification link sent"}


@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(
    data: VerifyEmailSchema, session: Session = Depends(get_session)
):
    stored_email = redis_client.get(f"verify:{data.token}")
    if not stored_email or stored_email != data.email:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    user.is_email_verified = True
    session.add(user)
    session.commit()
    redis_client.delete(f"verify:{data.token}")
    return {"detail": "email verified successfully"}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
async def request_password_reset(
    data: EmailSchema, session: Session = Depends(get_session)
):
    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    token = secrets.token_urlsafe(32)
    redis_client.set(f"reset:{token}", user.email, ex=30 * 60)
    link = f"{settings.APP_HOST}/reset-password?token={token}&email={user.email}"
    sent = send_email(
        user.email,
        "Reset your QueryMind password",
        render_reset_email(link),
    )
    if not sent:
        logger.info("PASSWORD_RESET_LINK: %s", link)
    return {"detail": "reset link sent"}


@router.post("/reset-password/confirm", status_code=status.HTTP_200_OK)
async def confirm_password_reset(
    data: ResetPasswordSchema, session: Session = Depends(get_session)
):
    stored_email = redis_client.get(f"reset:{data.token}")
    if not stored_email or stored_email != data.email:
        raise HTTPException(status_code=400, detail="invalid or expired token")
    user = session.exec(select(User).where(User.email == data.email)).first()
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    user.hashed_password = hash_password(data.password)
    session.add(user)
    session.commit()
    redis_client.delete(f"reset:{data.token}")
    return {"detail": "password reset successfully"}


# --- Google OAuth ---

@router.get("/google/url", status_code=status.HTTP_200_OK)
async def google_auth_url():
    """Return a Google OAuth URL with a CSRF state token stored in Redis."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail="google auth not configured")
    state = secrets.token_urlsafe(24)
    redis_client.set(f"oauth_state:{state}", "1", ex=10 * 60)
    return {"url": build_google_auth_url(state)}


@router.get("/google-auth", status_code=status.HTTP_307_TEMPORARY_REDIRECT)
async def google_auth(
    request: Request,
    session: Session = Depends(get_session),
    state: str = Query(default=""),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    user_agent = request.headers.get("user-agent", "")

    if error or not code:
        return RedirectResponse(
            url=f"{settings.APP_HOST}/signin?msg=google authentication failed",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return RedirectResponse(
            url=f"{settings.APP_HOST}/signin?msg=google auth not configured",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    # Validate the CSRF state token to prevent login CSRF.
    if not state or not redis_client.get(f"oauth_state:{state}"):
        return RedirectResponse(
            url=f"{settings.APP_HOST}/signin?msg=invalid oauth state",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )
    redis_client.delete(f"oauth_state:{state}")

    profile = await exchange_code_for_user(code)
    if not profile or not profile.get("email"):
        return RedirectResponse(
            url=f"{settings.APP_HOST}/signin?msg=google authentication failed",
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
        )

    email = profile["email"]
    user = session.exec(select(User).where(User.email == email)).first()
    if not user:
        user = User(
            email=email,
            auth_provider="google",
            is_email_verified=True,
        )
        session.add(user)
        session.commit()
        session.refresh(user)

    # Browsers drop cookies set on cross-site 307 redirect responses, so the
    # tokens are NOT set here. Instead, issue a short-lived opaque session code
    # that the frontend exchanges for cookies via POST /api/v1/auth/session.
    # TTL is 10 minutes: Render free instances cold-start slowly, so the code
    # must outlive the full redirect + frontend boot sequence.
    session_code = secrets.token_urlsafe(32)
    redis_client.set(f"oauth_session:{session_code}", str(user.id), ex=600)

    return RedirectResponse(
        url=(
            f"{settings.APP_HOST}/account?msg=google authentication success"
            f"&session={session_code}"
        ),
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.post("/session", status_code=status.HTTP_200_OK)
async def exchange_session(
    data: SessionExchangeSchema,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    """Exchange a short-lived OAuth session code for session cookies.

    The code is single-use and expires after 10 minutes. Cookies are set on a
    normal same-site JSON response so browsers store them reliably.
    """
    user_id = redis_client.get(f"oauth_session:{data.token}")
    if not user_id:
        logger.warning("OAuth session exchange failed: invalid or expired code")
        raise HTTPException(status_code=400, detail="invalid or expired session")
    redis_client.delete(f"oauth_session:{data.token}")

    user = session.get(User, int(user_id))
    if not user:
        logger.warning("OAuth session exchange failed: user %s not found", user_id)
        raise HTTPException(status_code=404, detail="user not found")

    _set_session_cookies(response, user, request.headers.get("user-agent", ""))
    logger.info("OAuth session exchanged for user %s", user.email)
    return {"detail": "session established"}
