"""Password hashing, opaque token generation, and input validation."""
from __future__ import annotations

import hashlib
import re
import secrets

import bcrypt

MIN_PASSWORD_LENGTH = 8
# bcrypt refuses inputs longer than 72 bytes rather than silently truncating,
# so we reject them up front with a clear message instead of surprising the user.
MAX_PASSWORD_BYTES = 72

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Compared against when no user matches, so that login timing does not reveal
# whether an email address is registered.
_DUMMY_HASH = bcrypt.hashpw(b"stash-dummy-password", bcrypt.gensalt())


class ValidationError(ValueError):
    """User-facing input problem; safe to render back to the browser."""


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalise_email(email)
    if not _EMAIL_RE.match(email):
        raise ValidationError("Please enter a valid email address.")
    return email


def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValidationError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes long."
        )
    return password


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password. Runs a throwaway comparison when the user does not
    exist so that both branches cost roughly the same wall-clock time."""
    candidate = (password or "").encode("utf-8")
    if len(candidate) > MAX_PASSWORD_BYTES:
        return False
    if not password_hash:
        bcrypt.checkpw(candidate, _DUMMY_HASH)
        return False
    try:
        return bcrypt.checkpw(candidate, password_hash.encode("utf-8"))
    except ValueError:
        # Stored hash is malformed; treat as a failed login rather than a 500.
        return False


def generate_token() -> str:
    """A high-entropy, URL-safe secret. Shown to the user exactly once."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored as SHA-256 digests, so a database leak does not hand
    over usable session cookies or reset links."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
