"""In-process chat transcript store, keyed by user id.

Deliberately not persisted. Transcripts are a convenience for multi-turn
phrasing ("delete that one"), not user data we promised to keep, and keeping
them out of Postgres avoided a table and a migration under time pressure.

Consequence, documented in the README: history resets when the process
restarts, and it would not be shared across multiple Render instances.
"""
from __future__ import annotations

from typing import Any

# Keep the tail of the conversation only, so a long session cannot grow the
# prompt (and the bill) without bound.
MAX_STORED_MESSAGES = 20

_transcripts: dict[str, list[dict[str, Any]]] = {}


def history(user_id: str) -> list[dict[str, Any]]:
    return list(_transcripts.get(str(user_id), []))


def record(user_id: str, user_message: str, assistant_reply: str) -> None:
    """Append one exchange. Only the plain-text turns are kept; tool calls stay
    within the turn that produced them."""
    key = str(user_id)
    transcript = _transcripts.setdefault(key, [])
    transcript.append({"role": "user", "content": user_message})
    transcript.append({"role": "assistant", "content": assistant_reply})
    del transcript[:-MAX_STORED_MESSAGES]


def clear(user_id: str) -> None:
    _transcripts.pop(str(user_id), None)
