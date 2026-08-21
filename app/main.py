"""Stash: FastAPI application entry point."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import auth_routes, chat_routes, db
from app.deps import AuthRequired, current_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    db.open_pool()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(title="Stash", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.exception_handler(AuthRequired)
async def handle_auth_required(request: Request, _: AuthRequired):
    """Browsers get bounced to the login page; the JSON API gets a 401."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "Your session expired."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


app.include_router(auth_routes.router)
app.include_router(chat_routes.router)


@app.get("/")
def index(request: Request):
    destination = "/chat" if current_user(request) else "/login"
    return RedirectResponse(destination, status_code=303)


@app.get("/healthz")
async def healthz():
    """Liveness probe for Render."""
    return {"status": "ok"}
