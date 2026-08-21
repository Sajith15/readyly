"""Signup, login, logout and the password-reset flow."""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import conversations, mailer, repository
from app.config import BASE_URL, SESSION_COOKIE_NAME
from app.deps import clear_session_cookie, current_user, set_session_cookie
from app.security import (
    ValidationError,
    hash_password,
    normalise_email,
    validate_email,
    validate_password,
    verify_password,
)
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _redirect(path: str) -> RedirectResponse:
    # 303 so the browser follows a POST with a GET.
    return RedirectResponse(path, status_code=303)


# --- signup ---------------------------------------------------------------


@router.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    if await current_user(request):
        return _redirect("/chat")
    return templates.TemplateResponse(request, "signup.html", {})


@router.post("/signup", response_class=HTMLResponse)
async def signup(
    request: Request,
    background: BackgroundTasks,
    email: str = Form(...),
    password: str = Form(...),
):
    try:
        email = validate_email(email)
        validate_password(password)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "signup.html", {"error": str(exc), "email": email}, status_code=400
        )

    user = await repository.create_user(email, hash_password(password))
    if user is None:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "That email is already registered.", "email": email},
            status_code=409,
        )

    background.add_task(mailer.send_welcome_email, email)

    token = await repository.create_session(str(user["id"]))
    response = _redirect("/chat")
    set_session_cookie(response, token)
    return response


# --- login / logout -------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, reset: int = 0):
    if await current_user(request):
        return _redirect("/chat")
    notice = "Password updated. Please sign in." if reset else None
    return templates.TemplateResponse(request, "login.html", {"notice": notice})


@router.post("/login", response_class=HTMLResponse)
async def login(
    request: Request, email: str = Form(...), password: str = Form(...)
):
    email = normalise_email(email)
    user = await repository.get_user_by_email(email)

    # verify_password burns an equivalent amount of time when the user is
    # missing, so this branch does not reveal which emails are registered.
    if not verify_password(password, user["password_hash"] if user else None):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect email or password.", "email": email},
            status_code=401,
        )

    token = await repository.create_session(str(user["id"]))
    response = _redirect("/chat")
    set_session_cookie(response, token)
    return response


@router.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        await repository.delete_session(token)
    response = _redirect("/login")
    clear_session_cookie(response)
    return response


# --- forgot / reset password ---------------------------------------------


@router.get("/forgot", response_class=HTMLResponse)
async def forgot_form(request: Request, sent: int = 0):
    return templates.TemplateResponse(request, "forgot.html", {"sent": bool(sent)})


@router.post("/forgot", response_class=HTMLResponse)
async def forgot(
    request: Request, background: BackgroundTasks, email: str = Form(...)
):
    email = normalise_email(email)
    user = await repository.get_user_by_email(email)

    if user is not None:
        token = await repository.create_reset_token(str(user["id"]))
        reset_url = f"{BASE_URL}/reset?token={token}"
        background.add_task(mailer.send_password_reset_email, email, reset_url)
    else:
        logger.info("Reset requested for unknown address; responding generically.")

    # Identical response either way: never confirm whether an account exists.
    return _redirect("/forgot?sent=1")


@router.get("/reset", response_class=HTMLResponse)
async def reset_form(request: Request, token: str = ""):
    if not token or not await repository.reset_token_is_valid(token):
        return templates.TemplateResponse(
            request, "reset.html", {"invalid": True}, status_code=400
        )
    return templates.TemplateResponse(request, "reset.html", {"token": token})


@router.post("/reset", response_class=HTMLResponse)
async def reset(
    request: Request, token: str = Form(...), password: str = Form(...)
):
    try:
        validate_password(password)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "reset.html", {"token": token, "error": str(exc)}, status_code=400
        )

    # Spending the token and changing the password are separate statements, but
    # the token is spent first: a failure after this point cannot leave a
    # reusable link behind.
    user_id = await repository.consume_reset_token(token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "reset.html", {"invalid": True}, status_code=400
        )

    await repository.set_password(user_id, hash_password(password))
    await repository.invalidate_reset_tokens_for_user(user_id)
    # Anyone holding an old session (including whoever forced the reset) is out.
    await repository.delete_sessions_for_user(user_id)
    conversations.clear(user_id)

    return _redirect("/login?reset=1")
