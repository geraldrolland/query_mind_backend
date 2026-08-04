"""SMTP email sending for verification, password reset, and other transactional mail."""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from app.core.settings import settings

logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, html_body: str, text_body: str | None = None) -> bool:
    """Send an HTML email via SMTP.

    Returns True if the message was accepted by the server, False otherwise.
    When SMTP is not configured this logs the message and returns False, so
    callers can fall back to dev-mode delivery.
    """
    if not settings.SMTP_HOST or not settings.SMTP_USERNAME:
        logger.warning(
            "SMTP not configured - email to %s skipped. Set SMTP_HOST/SMTP_USERNAME.",
            to_email,
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, settings.SMTP_FROM_EMAIL))
    msg["To"] = to_email
    msg.set_content(text_body or "Please view this email in an HTML-capable client.")
    msg.add_alternative(html_body, subtype="html")

    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            if settings.SMTP_USE_STARTTLS:
                context = ssl.create_default_context()
                server.starttls(context=context)
        with server:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
            server.send_message(msg)
        logger.info("Email sent to %s: %s", to_email, subject)
        return True
    except Exception as exc:
        logger.error("Failed to send email to %s: %s", to_email, exc)
        return False


def render_verification_email(link: str) -> str:
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">Verify your email</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">Welcome to QueryMind! Confirm your email address to activate your account and start exploring your datasets.</p>
      <p style="margin:24px 0">
        <a href="{link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600">Verify email address</a>
      </p>
      <p style="color:#6b7280;font-size:12px">This link expires in 30 minutes. If you didn't create a QueryMind account, you can ignore this email.</p>
    </div>
    """


def render_reset_email(link: str) -> str:
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;border:1px solid #e5e7eb;border-radius:12px">
      <h2 style="color:#111827;margin:0 0 8px">Reset your password</h2>
      <p style="color:#4b5563;font-size:14px;line-height:1.6">We received a request to reset the password for your QueryMind account. Click below to choose a new password.</p>
      <p style="margin:24px 0">
        <a href="{link}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:600">Reset password</a>
      </p>
      <p style="color:#6b7280;font-size:12px">This link expires in 30 minutes. If you didn't request a password reset, you can safely ignore this email.</p>
    </div>
    """
