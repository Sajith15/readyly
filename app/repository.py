"""SQL for users, sessions and password-reset tokens.

Bookmarks are deliberately absent: they are reachable only via the MCP server.
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


async def create_user(email: str, password_hash: str) -> dict[str, Any] | None:
    """Insert a user. Returns None if the email is already registered."""
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO users (email, password_hash)
                    VALUES (%s, %s)
                    RETURNING id, email, created_at
                    """,
                    (email, password_hash),
                )
            except UniqueViolation:
                return None
            return await cur.fetchone()


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, email, password_hash FROM users WHERE lower(email) = %s",
                (email,),
            )
            return await cur.fetchone()


async def set_password(user_id: str, password_hash: str) -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (password_hash, user_id),
            )


# --- sessions -------------------------------------------------------------


async def create_session(user_id: str) -> str:
    """Create a session row and return the raw token for the cookie."""
    token = generate_token()
    expires_at = _now() + timedelta(hours=SESSION_TTL_HOURS)
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO sessions (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, hash_token(token), expires_at),
            )
    return token


async def get_user_by_session(token: str) -> dict[str, Any] | None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT u.id, u.email
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > now()
                """,
                (hash_token(token),),
            )
            return await cur.fetchone()


async def delete_session(token: str) -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM sessions WHERE token_hash = %s", (hash_token(token),)
            )


async def delete_sessions_for_user(user_id: str) -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))


# --- password reset tokens ------------------------------------------------


async def create_reset_token(user_id: str) -> str:
    token = generate_token()
    expires_at = _now() + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, hash_token(token), expires_at),
            )
    return token


async def reset_token_is_valid(token: str) -> bool:
    """Check a token without spending it, so the reset form can be rendered."""
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM password_reset_tokens
                WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
                """,
                (hash_token(token),),
            )
            return await cur.fetchone() is not None


async def consume_reset_token(token: str) -> str | None:
    """Atomically spend a reset token.

    The UPDATE ... WHERE used_at IS NULL is the single-use guarantee: two
    concurrent submissions of the same link cannot both match.
    Returns the owning user id, or None if the token was missing, expired or
    already spent.
    """
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE password_reset_tokens
                SET used_at = now()
                WHERE token_hash = %s AND used_at IS NULL AND expires_at > now()
                RETURNING user_id
                """,
                (hash_token(token),),
            )
            row = await cur.fetchone()
            return str(row["user_id"]) if row else None


async def invalidate_reset_tokens_for_user(user_id: str) -> None:
    """Spend every outstanding token for a user, e.g. after a successful reset."""
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE password_reset_tokens SET used_at = now()
                WHERE user_id = %s AND used_at IS NULL
                """,
                (user_id,),
            )
