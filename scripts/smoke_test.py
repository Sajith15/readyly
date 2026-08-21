"""End-to-end smoke test for everything except the LLM itself.

Exercises the real code paths: bookmarks go through the MCP bridge exactly as
the chat handler drives them, and auth goes through app.repository.

    python -m scripts.smoke_test

Requires DATABASE_URL and an initialised schema. Creates throwaway users and
deletes them (and their bookmarks, by cascade) on the way out.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid

from app import db, repository
from app.mcp_bridge import toolbox_for_user
from app.security import hash_password, verify_password

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)


def make_user(prefix: str, password: str = "correct horse") -> dict:
    return repository.create_user(
        f"{prefix}-{uuid.uuid4().hex[:8]}@example.test", hash_password(password)
    )


async def call(toolbox, name: str, **arguments):
    """Invoke an MCP tool the way the chat handler does, and decode the result."""
    return json.loads(await toolbox.call(name, arguments))


async def test_bookmarks_are_scoped_per_user() -> None:
    print("\nBookmarks via MCP, scoped per user")

    alice = make_user("alice")
    bob = make_user("bob")
    alice_id, bob_id = str(alice["id"]), str(bob["id"])

    try:
        async with toolbox_for_user(alice_id) as toolbox:
            tools = await toolbox.openai_tools()
            leaky = [
                field
                for tool in tools
                for field in tool["function"]["parameters"].get("properties", {})
                if "user" in field.lower()
            ]
            check("no tool schema exposes a user id", not leaky, str(leaky))

            saved = await call(
                toolbox,
                "add_bookmark",
                url="example.com/python-guide",
                title="Python guide",
                tags=["reading", "python"],
                notes="For the weekend",
            )
            bookmark = saved["saved"]
            check("add_bookmark stores a bookmark", "id" in bookmark)
            check(
                "a bare host is normalised to https",
                bookmark["url"] == "https://example.com/python-guide",
                bookmark["url"],
            )
            check("tags are stored", bookmark["tags"] == ["reading", "python"])

            listed = await call(toolbox, "list_bookmarks")
            check("the owner sees their bookmark", listed["count"] == 1)

            found = await call(toolbox, "search_bookmarks", query="python")
            check("keyword search matches", found["count"] == 1)

            by_tag = await call(toolbox, "search_bookmarks", tag="reading")
            check("tag search matches", by_tag["count"] == 1)

            missing = await call(toolbox, "search_bookmarks", query="kubernetes")
            check("an unrelated keyword returns nothing", missing["count"] == 0)

        alice_bookmark_id = bookmark["id"]

        async with toolbox_for_user(bob_id) as toolbox:
            listed = await call(toolbox, "list_bookmarks")
            check(
                "a second user sees none of the first user's bookmarks",
                listed["count"] == 0,
                str(listed),
            )

            found = await call(toolbox, "search_bookmarks", query="python")
            check("cross-user search returns nothing", found["count"] == 0)

            stolen = await call(toolbox, "delete_bookmark", bookmark_id=alice_bookmark_id)
            check(
                "a user cannot delete another user's bookmark, even with its id",
                stolen["deleted"] is False,
                str(stolen),
            )

            junk = await call(toolbox, "delete_bookmark", bookmark_id="not-a-uuid")
            check("a malformed id is rejected cleanly", junk["deleted"] is False)

        async with toolbox_for_user(alice_id) as toolbox:
            survived = await call(toolbox, "list_bookmarks")
            check("the targeted bookmark survived", survived["count"] == 1)

            deleted = await call(toolbox, "delete_bookmark", bookmark_id=alice_bookmark_id)
            check("the owner can delete it", deleted["deleted"] is True)

            empty = await call(toolbox, "list_bookmarks")
            check("it is gone afterwards", empty["count"] == 0)
    finally:
        repository.delete_users([alice_id, bob_id])


def test_password_reset_tokens() -> None:
    print("\nPassword reset tokens")

    user = make_user("reset", password="original pass")
    user_id = str(user["id"])

    try:
        token = repository.create_reset_token(user_id)
        check("a fresh token validates", repository.reset_token_is_valid(token))

        check("consuming returns the owner", repository.consume_reset_token(token) == user_id)
        check("the same token cannot be spent twice",
              repository.consume_reset_token(token) is None)
        check("a spent token no longer validates",
              not repository.reset_token_is_valid(token))
        check("an unknown token is rejected",
              repository.consume_reset_token("fabricated") is None)

        repository.set_password(user_id, hash_password("brand new pass"))
        refreshed = repository.get_user_by_email(user["email"])
        check("the new password works",
              verify_password("brand new pass", refreshed["password_hash"]))
        check("the old password no longer works",
              not verify_password("original pass", refreshed["password_hash"]))
    finally:
        repository.delete_users([user_id])


def test_sessions() -> None:
    print("\nSessions")

    user = make_user("session")
    user_id = str(user["id"])

    try:
        token = repository.create_session(user_id)
        resolved = repository.get_user_by_session(token)
        check("a session resolves to its user",
              resolved is not None and str(resolved["id"]) == user_id)

        repository.delete_session(token)
        check("logout revokes the session",
              repository.get_user_by_session(token) is None)

        second = repository.create_session(user_id)
        repository.delete_sessions_for_user(user_id)
        check("a password reset revokes every session",
              repository.get_user_by_session(second) is None)
        check("a fabricated cookie resolves to nobody",
              repository.get_user_by_session("fabricated") is None)
    finally:
        repository.delete_users([user_id])


def test_signup_rules() -> None:
    print("\nSignup")

    user = make_user("dupe")
    try:
        check("the first signup succeeds", user is not None)
        duplicate = repository.create_user(user["email"], hash_password("other password"))
        check("a duplicate email is rejected", duplicate is None)
    finally:
        repository.delete_users([str(user["id"])])


def main() -> int:
    db.open_pool()
    try:
        test_signup_rules()
        test_sessions()
        test_password_reset_tokens()
        # Only the bookmark path needs an event loop: it talks to the MCP
        # subprocess over stdio.
        asyncio.run(test_bookmarks_are_scoped_per_user())
    finally:
        db.close_pool()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed: {', '.join(_failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
