"""Stash MCP server (stdio transport).

Security model
--------------
The owning user is pinned to this process via the STASH_USER_ID environment
variable, which the web app sets when it spawns the subprocess. None of the
tool signatures below accept a user id, so the tool schemas the LLM sees have
no field in which to name another user. Prompt injection cannot widen scope,
because the scope is not part of the model's vocabulary.

Run manually with:
    STASH_USER_ID=<uuid> DATABASE_URL=... python -m mcp_server.server
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server import MCPServer

from mcp_server import repository

mcp = MCPServer(
    name="stash-bookmarks",
    version="1.0.0",
    instructions="Read and write the signed-in user's bookmarks.",
)


def _user_id() -> str:
    user_id = os.environ.get("STASH_USER_ID", "").strip()
    if not user_id:
        raise RuntimeError(
            "STASH_USER_ID is not set; refusing to serve unscoped bookmark tools."
        )
    return user_id


@mcp.tool(
    description=(
        "Save a new bookmark for the user. Provide the URL, and optionally a "
        "short title, a list of tags, and free-form notes."
    )
)
def add_bookmark(
    url: str,
    title: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    bookmark = repository.add_bookmark(
        user_id=_user_id(), url=url, title=title, tags=tags, notes=notes
    )
    return {"saved": bookmark}


@mcp.tool(
    description=(
        "List the user's bookmarks, most recently saved first. Use this when "
        "the user asks what they have saved without naming a specific topic."
    )
)
def list_bookmarks(limit: int = 20) -> dict[str, Any]:
    bookmarks = repository.list_bookmarks(user_id=_user_id(), limit=limit)
    return {"count": len(bookmarks), "bookmarks": bookmarks}


@mcp.tool(
    description=(
        "Search the user's bookmarks by free-text keyword (matched against "
        "URL, title, notes and tags) and/or by an exact tag. Call this before "
        "deleting so you can resolve the correct bookmark id."
    )
)
def search_bookmarks(
    query: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    bookmarks = repository.search_bookmarks(
        user_id=_user_id(), query=query, tag=tag, limit=limit
    )
    return {"count": len(bookmarks), "bookmarks": bookmarks}


@mcp.tool(
    description=(
        "Delete one of the user's bookmarks by its id. Look the id up with "
        "search_bookmarks or list_bookmarks first; never guess an id."
    )
)
def delete_bookmark(bookmark_id: str) -> dict[str, Any]:
    deleted = repository.delete_bookmark(user_id=_user_id(), bookmark_id=bookmark_id)
    if deleted is None:
        return {
            "deleted": False,
            "reason": "No bookmark with that id belongs to this user.",
        }
    return {"deleted": True, "bookmark": deleted}


if __name__ == "__main__":
    mcp.run("stdio")
