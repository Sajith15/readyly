"""Signup, login, logout and the password-reset flow.

Handlers are `def` rather than `async def`: they are database-bound, so FastAPI
runs them in a threadpool. Email is dispatched as a background task so a slow
Resend call never delays the redirect.
"""
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
def signup_form(request: Request):
    if current_user(request):
        return _redirect("/chat")
    return templates.TemplateResponse(request, "signup.html", {})


@router.post("/signup", response_class=HTMLResponse)
def signup(
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

    user = repository.create_user(email, hash_password(password))
    if user is None:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "That email is already registered.", "email": email},
            status_code=409,
        )

    background.add_task(mailer.send_welcome_email, email)

    token = repository.create_session(str(user["id"]))
    response = _redirect("/chat")
    set_session_cookie(response, token)
    return response


# --- login / logout -------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, reset: int = 0):
    if current_user(request):
        return _redirect("/chat")
    notice = "Password updated. Please sign in." if reset else None
    return templates.TemplateResponse(request, "login.html", {"notice": notice})


@router.post("/login", response_class=HTMLResponse)
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    email = normalise_email(email)
    user = repository.get_user_by_email(email)

    # verify_password burns an equivalent amount of time when the user is
    # missing, so this branch does not reveal which emails are registered.
    if not verify_password(password, user["password_hash"] if user else None):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Incorrect email or password.", "email": email},
            status_code=401,
        )

    token = repository.create_session(str(user["id"]))
    response = _redirect("/chat")
    set_session_cookie(response, token)
    return response


@router.post("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        repository.delete_session(token)
    response = _redirect("/login")
    clear_session_cookie(response)
    return response


# --- forgot / reset password ---------------------------------------------


@router.get("/forgot", response_class=HTMLResponse)
def forgot_form(request: Request, sent: int = 0):
    return templates.TemplateResponse(request, "forgot.html", {"sent": bool(sent)})


@router.post("/forgot", response_class=HTMLResponse)
def forgot(request: Request, background: BackgroundTasks, email: str = Form(...)):
    email = normalise_email(email)
    user = repository.get_user_by_email(email)

    if user is not None:
        token = repository.create_reset_token(str(user["id"]))
        reset_url = f"{BASE_URL}/reset?token={token}"
        background.add_task(mailer.send_password_reset_email, email, reset_url)
    else:
        logger.info("Reset requested for unknown address; responding generically.")

    # Identical response either way: never confirm whether an account exists.
    return _redirect("/forgot?sent=1")


@router.get("/reset", response_class=HTMLResponse)
def reset_form(request: Request, token: str = ""):
    if not token or not repository.reset_token_is_valid(token):
        return templates.TemplateResponse(
            request, "reset.html", {"invalid": True}, status_code=400
        )
    return templates.TemplateResponse(request, "reset.html", {"token": token})


@router.post("/reset", response_class=HTMLResponse)
def reset(request: Request, token: str = Form(...), password: str = Form(...)):
    try:
        validate_password(password)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "reset.html", {"token": token, "error": str(exc)}, status_code=400
        )

    # The token is spent before the password changes, so a failure part-way
    # through cannot leave a reusable link behind.
    user_id = repository.consume_reset_token(token)
    if user_id is None:
        return templates.TemplateResponse(
            request, "reset.html", {"invalid": True}, status_code=400
        )

    repository.set_password(user_id, hash_password(password))
    repository.invalidate_reset_tokens_for_user(user_id)
    # Anyone holding an old session (including whoever forced the reset) is out.
    repository.delete_sessions_for_user(user_id)
    conversations.clear(user_id)

    return _redirect("/login?reset=1")
