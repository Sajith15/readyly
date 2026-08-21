"""All SQL against the `bookmarks` table.

This module is imported only by the MCP server process. The web/chat layer
reaches bookmarks exclusively through MCP tool calls.

Every statement here takes `user_id` as its first filter. There is no code path
that reads or writes a bookmark without scoping it to a single owner.
"""
from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

_COLUMNS = "id, user_id, url, title, tags, notes, created_at"


def _connect() -> psycopg.Connection:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set for the MCP server process.")
    return psycopg.connect(database_url, row_factory=dict_row)


def _serialise(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a DB row into JSON-safe primitives for the MCP wire format."""
    return {
        "id": str(row["id"]),
        "url": row["url"],
        "title": row["title"],
        "tags": list(row["tags"] or []),
        "notes": row["notes"],
        "created_at": row["created_at"].isoformat(),
    }


def _clean_tags(tags: list[str] | None) -> list[str]:
    if not tags:
        return []
    seen: dict[str, str] = {}
    for raw in tags:
        tag = (raw or "").strip().lstrip("#")
        if tag and tag.lower() not in seen:
            seen[tag.lower()] = tag
    return list(seen.values())


def add_bookmark(
    user_id: str,
    url: str,
    title: str | None = None,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    url = (url or "").strip()
    if not url:
        raise ValueError("A bookmark needs a URL.")
    if "://" not in url:
        url = f"https://{url}"

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO bookmarks (user_id, url, title, tags, notes)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (user_id, url, (title or "").strip(), _clean_tags(tags), (notes or "").strip()),
        )
        row = cur.fetchone()
        conn.commit()
    return _serialise(row)


def list_bookmarks(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_COLUMNS} FROM bookmarks
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (user_id, limit),
        )
        return [_serialise(row) for row in cur.fetchall()]


def search_bookmarks(
    user_id: str,
    query: str | None = None,
    tag: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    conditions = ["user_id = %s"]
    params: list[Any] = [user_id]

    query = (query or "").strip()
    if query:
        like = f"%{query}%"
        conditions.append(
            "(url ILIKE %s OR title ILIKE %s OR notes ILIKE %s"
            " OR EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE t ILIKE %s))"
        )
        params.extend([like, like, like, like])

    tag = (tag or "").strip().lstrip("#")
    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM unnest(tags) AS t WHERE lower(t) = lower(%s))"
        )
        params.append(tag)

    params.append(limit)
    where = " AND ".join(conditions)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT {_COLUMNS} FROM bookmarks WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %s",
            params,
        )
        return [_serialise(row) for row in cur.fetchall()]


def delete_bookmark(user_id: str, bookmark_id: str) -> dict[str, Any] | None:
    """Delete one bookmark. Returns the deleted row, or None if the caller does
    not own a bookmark with that id."""
    try:
        # Models occasionally invent ids; reject non-UUIDs before they reach
        # Postgres and abort the transaction.
        bookmark_id = str(UUID(str(bookmark_id)))
    except (ValueError, AttributeError, TypeError):
        return None

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM bookmarks WHERE id = %s AND user_id = %s RETURNING {_COLUMNS}",
            (bookmark_id, user_id),
        )
        row = cur.fetchone()
        conn.commit()
    return _serialise(row) if row else None
