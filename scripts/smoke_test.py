"""End-to-end smoke test for everything except the LLM itself.

Exercises the real code paths: bookmarks go through the MCP bridge exactly as
the chat handler would drive them, and auth goes through app.repository.

    python -m scripts.smoke_test

Requires DATABASE_URL and an initialised schema. Creates two throwaway users
and deletes them (and their bookmarks, by cascade) on the way out.
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


async def call(toolbox, name: str, **arguments):
    return json.loads(await toolbox.call(name, arguments))


async def test_bookmarks_are_scoped_per_user() -> None:
    print("\nBookmarks via MCP, scoped per user")

    alice = await repository.create_user(
        f"alice-{uuid.uuid4().hex[:8]}@example.test", hash_password("correct horse")
    )
    bob = await repository.create_user(
        f"bob-{uuid.uuid4().hex[:8]}@example.test", hash_password("battery staple")
    )
    alice_id, bob_id = str(alice["id"]), str(bob["id"])

    try:
        async with toolbox_for_user(alice_id) as toolbox:
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
                "bare host is normalised to https",
                bookmark["url"] == "https://example.com/python-guide",
                bookmark["url"],
            )
            check("tags are stored", bookmark["tags"] == ["reading", "python"])

            listed = await call(toolbox, "list_bookmarks")
            check("owner sees their bookmark", listed["count"] == 1)

            found = await call(toolbox, "search_bookmarks", query="python")
            check("keyword search matches", found["count"] == 1)

            by_tag = await call(toolbox, "search_bookmarks", tag="reading")
            check("tag search matches", by_tag["count"] == 1)

            missing = await call(toolbox, "search_bookmarks", query="kubernetes")
            check("unrelated keyword returns nothing", missing["count"] == 0)

        alice_bookmark_id = bookmark["id"]

        async with toolbox_for_user(bob_id) as toolbox:
            listed = await call(toolbox, "list_bookmarks")
            check("a second user sees none of the first user's bookmarks",
                  listed["count"] == 0, str(listed))

            found = await call(toolbox, "search_bookmarks", query="python")
            check("cross-user search returns nothing", found["count"] == 0)

            stolen = await call(
                toolbox, "delete_bookmark", bookmark_id=alice_bookmark_id
            )
            check(
                "a user cannot delete another user's bookmark by id",
                stolen["deleted"] is False,
                str(stolen),
            )

            junk = await call(toolbox, "delete_bookmark", bookmark_id="not-a-uuid")
            check("a malformed id is rejected cleanly", junk["deleted"] is False)

        async with toolbox_for_user(alice_id) as toolbox:
            still_there = await call(toolbox, "list_bookmarks")
            check("the targeted bookmark survived", still_there["count"] == 1)

            deleted = await call(
                toolbox, "delete_bookmark", bookmark_id=alice_bookmark_id
            )
            check("the owner can delete it", deleted["deleted"] is True)

            empty = await call(toolbox, "list_bookmarks")
            check("it is gone afterwards", empty["count"] == 0)
    finally:
        await _delete_users(alice_id, bob_id)


async def test_password_reset_tokens() -> None:
    print("\nPassword reset tokens")

    user = await repository.create_user(
        f"reset-{uuid.uuid4().hex[:8]}@example.test", hash_password("original pass")
    )
    user_id = str(user["id"])

    try:
        token = await repository.create_reset_token(user_id)
        check("a fresh token validates", await repository.reset_token_is_valid(token))

        spent = await repository.consume_reset_token(token)
        check("consuming returns the owner", spent == user_id)

        replay = await repository.consume_reset_token(token)
        check("the same token cannot be spent twice", replay is None)
        check(
            "a spent token no longer validates",
            not await repository.reset_token_is_valid(token),
        )
        check(
            "an unknown token is rejected",
            await repository.consume_reset_token("fabricated") is None,
        )

        await repository.set_password(user_id, hash_password("brand new pass"))
        refreshed = await repository.get_user_by_email(user["email"])
        check(
            "the new password works",
            verify_password("brand new pass", refreshed["password_hash"]),
        )
        check(
            "the old password no longer works",
            not verify_password("original pass", refreshed["password_hash"]),
        )
    finally:
        await _delete_users(user_id)


async def test_sessions() -> None:
    print("\nSessions")

    user = await repository.create_user(
        f"session-{uuid.uuid4().hex[:8]}@example.test", hash_password("some password")
    )
    user_id = str(user["id"])

    try:
        token = await repository.create_session(user_id)
        resolved = await repository.get_user_by_session(token)
        check("a session resolves to its user", resolved and str(resolved["id"]) == user_id)

        await repository.delete_session(token)
        check(
            "logout revokes the session",
            await repository.get_user_by_session(token) is None,
        )

        second = await repository.create_session(user_id)
        await repository.delete_sessions_for_user(user_id)
        check(
            "a password reset revokes every session",
            await repository.get_user_by_session(second) is None,
        )
        check(
            "a fabricated cookie resolves to nobody",
            await repository.get_user_by_session("fabricated") is None,
        )
    finally:
        await _delete_users(user_id)


async def test_duplicate_signup_is_rejected() -> None:
    print("\nSignup")

    email = f"dupe-{uuid.uuid4().hex[:8]}@example.test"
    first = await repository.create_user(email, hash_password("some password"))
    check("the first signup succeeds", first is not None)

    try:
        second = await repository.create_user(email, hash_password("other password"))
        check("a duplicate email is rejected", second is None)
    finally:
        await _delete_users(str(first["id"]))


async def _delete_users(*user_ids: str) -> None:
    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM users WHERE id = ANY(%s)", (list(user_ids),)
            )


async def main() -> int:
    await db.open_pool()
    try:
        await test_duplicate_signup_is_rejected()
        await test_sessions()
        await test_password_reset_tokens()
        await test_bookmarks_are_scoped_per_user()
    finally:
        await db.close_pool()

    print()
    if _failures:
        print(f"{len(_failures)} check(s) failed: {', '.join(_failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
