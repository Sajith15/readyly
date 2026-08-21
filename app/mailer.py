"""Transactional email via Resend.

Email is best-effort: a Resend outage must not stop somebody signing up or
requesting a reset link. Failures are logged with context rather than raised,
and never surfaced to the user in a way that would leak whether an account
exists.

When RESEND_API_KEY is unset (local development) the message is logged instead
of sent, so the whole flow stays runnable without credentials.
"""
from __future__ import annotations

import logging

import resend

from app.config import BASE_URL, EMAIL_FROM, RESEND_API_KEY, RESET_TOKEN_TTL_MINUTES

logger = logging.getLogger(__name__)

if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY


_STYLE = (
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;"
    "line-height:1.6;color:#1f2430;"
)
_BUTTON = (
    "display:inline-block;padding:12px 22px;background:#4f46e5;color:#ffffff;"
    "text-decoration:none;border-radius:8px;font-weight:600;"
)


async def _send(to: str, subject: str, html: str, text: str) -> None:
    if not RESEND_API_KEY:
        logger.warning(
            "RESEND_API_KEY not set - skipping real send. To=%s Subject=%s\n%s",
            to,
            subject,
            text,
        )
        return

    try:
        response = await resend.Emails.send_async(
            {
                "from": EMAIL_FROM,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text,
            }
        )
        logger.info("Sent %r to %s (resend id=%s)", subject, to, response.get("id"))
    except Exception:
        # Deliberately not re-raised: the caller's flow (signup, reset request)
        # is still valid even if the notification failed.
        logger.exception("Failed to send %r to %s", subject, to)


async def send_welcome_email(to: str) -> None:
    subject = "Welcome to Stash"
    html = f"""
    <div style="{_STYLE}">
      <h2>Welcome to Stash</h2>
      <p>Your account is ready. Stash is a bookmark manager you talk to.</p>
      <p>Try asking your co-pilot things like:</p>
      <ul>
        <li>&ldquo;Save https://example.com under tag reading&rdquo;</li>
        <li>&ldquo;What did I stash about Python?&rdquo;</li>
        <li>&ldquo;Delete the example.com bookmark&rdquo;</li>
      </ul>
      <p><a href="{BASE_URL}/chat" style="{_BUTTON}">Open Stash</a></p>
    </div>
    """
    text = (
        "Welcome to Stash!\n\n"
        "Your account is ready. Stash is a bookmark manager you talk to.\n\n"
        'Try: "Save https://example.com under tag reading"\n'
        f"Open Stash: {BASE_URL}/chat\n"
    )
    await _send(to, subject, html, text)


async def send_password_reset_email(to: str, reset_url: str) -> None:
    subject = "Reset your Stash password"
    html = f"""
    <div style="{_STYLE}">
      <h2>Reset your password</h2>
      <p>Click the button below to choose a new password. This link can be
         used once and expires in {RESET_TOKEN_TTL_MINUTES} minutes.</p>
      <p><a href="{reset_url}" style="{_BUTTON}">Set a new password</a></p>
      <p style="color:#6b7280;font-size:13px">
        If the button does not work, paste this into your browser:<br>
        <a href="{reset_url}">{reset_url}</a>
      </p>
      <p style="color:#6b7280;font-size:13px">
        If you did not request this, you can safely ignore this email.
      </p>
    </div>
    """
    text = (
        "Reset your Stash password\n\n"
        f"Use this link within {RESET_TOKEN_TTL_MINUTES} minutes (single use):\n"
        f"{reset_url}\n\n"
        "If you did not request this, ignore this email.\n"
    )
    await _send(to, subject, html, text)
