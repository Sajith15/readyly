"""Request-scoped authentication helpers."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from app import repository
from app.config import IS_PRODUCTION, SESSION_COOKIE_NAME, SESSION_TTL_HOURS


class AuthRequired(Exception):
    """Raised by protected routes when there is no valid session."""


async def current_user(request: Request) -> dict[str, Any] | None:
    """Resolve the signed-in user from the session cookie, or None."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return await repository.get_user_by_session(token)


async def require_user(request: Request) -> dict[str, Any]:
    user = await current_user(request)
    if user is None:
        raise AuthRequired()
    return user


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=SESSION_TTL_HOURS * 3600,
        httponly=True,
        samesite="lax",
        secure=IS_PRODUCTION,
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
