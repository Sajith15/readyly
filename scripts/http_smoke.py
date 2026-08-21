"""HTTP-level walkthrough of the auth flows, run in-process against the ASGI app.

Covers the interviewer's click-through minus the LLM: signup, session gating,
logout, the forgot -> emailed link -> reset -> old password dies sequence, and
single-use enforcement on the reset link.

    python -m scripts.http_smoke

The reset link is read out of the mailer's log line (the mailer logs instead of
sending when RESEND_API_KEY is unset), so this exercises the real email path
rather than reaching into the tokens table.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid

import httpx

from app import db, repository
from app.main import app

BASE = "http://testserver"

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)


class MailCapture(logging.Handler):
    """Collects the mailer's log output so the test can read the reset link."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())

    def latest_reset_path(self) -> str | None:
        """The emailed link is absolute against BASE_URL; the test only needs
        the path to replay it against the in-process app."""
        for message in reversed(self.messages):
            match = re.search(r"/reset\?token=(\S+)", message)
            if match:
                return match.group(0)
        return None


async def main() -> int:
    db.open_pool()

    capture = MailCapture()
    logging.getLogger("app.mailer").addHandler(capture)

    email = f"walkthrough-{uuid.uuid4().hex[:8]}@example.test"
    old_password = "first password"
    new_password = "second password"
    user_id: str | None = None

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE, follow_redirects=False
        ) as client:
            print("\nSignup and session")
            response = await client.get("/login")
            check("the login page renders", response.status_code == 200)

            response = await client.post(
                "/signup", data={"email": email, "password": old_password}
            )
            check("signup redirects to the chat", response.status_code == 303
                  and response.headers["location"] == "/chat", str(response.status_code))
            check("a session cookie is set", "stash_session" in response.cookies)

            record = repository.get_user_by_email(email)
            check("the user exists in the database", record is not None)
            user_id = str(record["id"]) if record else None

            check("a welcome email was dispatched",
                  any("Welcome to Stash" in m for m in capture.messages))

            response = await client.get("/chat")
            check("the chat page renders for a signed-in user",
                  response.status_code == 200 and email in response.text)

            print("\nWeak credentials are rejected")
            short = await client.post(
                "/signup", data={"email": f"x{uuid.uuid4().hex[:6]}@example.test",
                                 "password": "short"}
            )
            check("a too-short password is rejected", short.status_code == 400)

            duplicate = await client.post(
                "/signup", data={"email": email, "password": old_password}
            )
            check("a duplicate signup is rejected", duplicate.status_code == 409)

            print("\nLogout gates access")
            response = await client.post("/logout")
            check("logout redirects to login", response.status_code == 303)

            response = await client.get("/chat")
            check("the chat page is gated once logged out",
                  response.status_code == 303
                  and response.headers["location"] == "/login")

            response = await client.post("/api/chat", json={"message": "hello"})
            check("the chat API returns 401 without a session",
                  response.status_code == 401)

            print("\nForgot password")
            response = await client.post("/forgot", data={"email": email})
            check("the reset request redirects", response.status_code == 303)

            unknown = await client.post(
                "/forgot", data={"email": "nobody@example.test"}
            )
            check("an unknown address gets an identical response",
                  unknown.status_code == response.status_code
                  and unknown.headers["location"] == response.headers["location"])

            path = capture.latest_reset_path()
            check("a reset link was emailed", path is not None)
            if not path:
                return 1

            response = await client.get(path)
            check("the reset link opens the form", response.status_code == 200)

            bad = await client.get("/reset?token=fabricated")
            check("a fabricated token is refused", bad.status_code == 400)

            token = path.split("token=", 1)[1]
            response = await client.post(
                "/reset", data={"token": token, "password": new_password}
            )
            check("setting a new password redirects to login",
                  response.status_code == 303
                  and response.headers["location"] == "/login?reset=1")

            print("\nThe reset actually took effect")
            response = await client.post(
                "/login", data={"email": email, "password": old_password}
            )
            check("the old password no longer works", response.status_code == 401)

            response = await client.post(
                "/login", data={"email": email, "password": new_password}
            )
            check("the new password works", response.status_code == 303)

            replay = await client.post(
                "/reset", data={"token": token, "password": "third password"}
            )
            check("the reset link cannot be reused", replay.status_code == 400)

            response = await client.post(
                "/login", data={"email": email, "password": "third password"}
            )
            check("the replayed reset did not change anything",
                  response.status_code == 401)
    finally:
        logging.getLogger("app.mailer").removeHandler(capture)
        if user_id:
            repository.delete_users([user_id])
        db.close_pool()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed: {', '.join(_failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
