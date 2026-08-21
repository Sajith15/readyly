"""SQL for users, sessions and password-reset tokens.

Bookmarks are deliberately absent: they are reachable only via the MCP server.
These functions are synchronous and are called from `def` routes and
dependencies, which FastAPI runs in a threadpool. See app/db.py for why.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.errors import UniqueViolation

from app import db
from app.config import RESET_TOKEN_TTL_MINUTES, SESSION_TTL_HOURS
from app.security import generate_token, hash_token


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- users ----------------------------------------------------------------


def create_user(email: str, password_hash: str) -> dict[str, Any] | None:
    """Insert a user. Returns None if the email is already registered."""
    with db.connection() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                """
                INSERT INTO users (email, password_hash)
                VALUES (%s, %s)
                RETURNING id, email, created_at
                """,
                (email, password_hash),
            )
        except UniqueViolation:
            return None
        return cur.fetchone()


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, password_hash FROM users WHERE lower(email) = %s",
            (email,),
        )
        return cur.fetchone()


def set_password(user_id: str, password_hash: str) -> None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (password_hash, user_id),
        )


# --- sessions -------------------------------------------------------------


def create_session(user_id: str) -> str:
    """Create a session row and return the raw token for the cookie."""
    token = generate_token()
    expires_at = _now() + timedelta(hours=SESSION_TTL_HOURS)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_id, hash_token(token), expires_at),
        )
    return token


def get_user_by_session(token: str) -> dict[str, Any] | None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id, u.email
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = %s AND s.expires_at > now()
            """,
            (hash_token(token),),
        )
        return cur.fetchone()


def delete_session(token: str) -> None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),))


def delete_sessions_for_user(user_id: str) -> None:
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


# --- password reset tokens ------------------------------------------------


def create_reset_token(user_id: str) -> str:
    token = generate_token()
    expires_at = _now() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
            VALUES (%s, %s, %s)
            """,
            (user_id, hash_token(token), expires_at),
        )
    return token


def reset_token_is_valid(token: str) -> bool:
    """Check a token without spending it, so the reset form can be rendered."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM password_reset_tokens
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
            """,
            (hash_token(token),),
        )
        return cur.fetchone() is not None


def consume_reset_token(token: str) -> str | None:
    """Atomically spend a reset token.

    The `WHERE used_at IS NULL` is the single-use guarantee: two concurrent
    submissions of the same link cannot both match. Returns the owning user id,
    or None if the token was missing, expired or already spent.
    """
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE password_reset_tokens
            SET used_at = now()
            WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
            RETURNING user_id
            """,
            (hash_token(token),),
        )
        row = cur.fetchone()
        return str(row["user_id"]) if row else None


def invalidate_reset_tokens_for_user(user_id: str) -> None:
    """Spend every outstanding token for a user, e.g. after a successful reset."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE password_reset_tokens SET used_at = now()
            WHERE user_id = %s AND used_at IS NULL
            """,
            (user_id,),
        )


def delete_users(user_ids: list[str]) -> None:
    """Remove users and, by cascade, everything they own. Used by the smoke test."""
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = ANY(%s)", (user_ids,))
