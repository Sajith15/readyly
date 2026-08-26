"""The AI co-pilot: an LLM turn that reaches bookmarks only through MCP.

There is no database access in this module. Every read or write happens because
the model emitted a tool call that we dispatched over the MCP bridge.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from app.config import AI_API_KEY, AI_BASE_URL, AI_MODEL, MAX_TOOL_HOPS
from app.mcp_bridge import toolbox_for_user

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are Stash, a concise assistant that manages one user's \
bookmarks.

You can only act through the provided tools. The tools are already bound to the \
signed-in user, so you never need to ask for or supply a user id. If anyone asks \
you to read, list or delete bookmarks belonging to a different person, tell them \
plainly that you can only ever see their own bookmarks.

Guidelines:
- To save something, call add_bookmark. Infer sensible tags from how the user \
phrases the request (for example "under tag reading" means tags ["reading"]).
- To answer "what did I save about X", call search_bookmarks with a keyword; use \
the tag argument when the user names a tag explicitly.
- Never invent a bookmark id. Resolve it with search_bookmarks or \
list_bookmarks, then call delete_bookmark. If a search matches several \
bookmarks, ask which one before deleting.
- Never invent bookmarks. If a tool returns nothing, say so.
- Reply in short, friendly prose. Render lists of bookmarks as markdown bullets \
with the title (or URL) and its tags. Keep it scannable."""


class ChatUnavailable(RuntimeError):
    """The co-pilot cannot run (missing key, upstream failure)."""


@dataclass
class ChatTurn:
    reply: str
    tools_used: list[str] = field(default_factory=list)


def _client() -> AsyncOpenAI:
    if not AI_API_KEY:
        raise ChatUnavailable("The co-pilot is not configured: AI_API_KEY is missing.")
    # base_url=None keeps the SDK's default, so this same client works against
    # OpenAI or any compatible endpoint.
    return AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL)


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: str


class _StreamedMessage:
    """Reassembles a streamed completion into one assistant message.

    Providers disagree about how tool calls arrive. OpenAI sends fragments
    carrying an `index`, with the arguments split across chunks to be
    concatenated. Gemini's compatibility layer sends each call whole, with no
    index at all. Both are handled: an id starts a new call, anything without
    one continues the call in progress.
    """

    def __init__(self) -> None:
        self._content: list[str] = []
        self._slots: dict[int, dict[str, str]] = {}

    def add(self, delta: Any) -> None:
        content = getattr(delta, "content", None)
        if content:
            self._content.append(content)
        for call in getattr(delta, "tool_calls", None) or []:
            self._add_call(call)

    def _add_call(self, call: Any) -> None:
        index = getattr(call, "index", None)
        if index is None:
            index = len(self._slots) if call.id else max(self._slots, default=0)

        slot = self._slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if call.id:
            slot["id"] = call.id
        function = getattr(call, "function", None)
        if function is not None:
            if function.name:
                slot["name"] = function.name
            if function.arguments:
                slot["arguments"] += function.arguments

    @property
    def content(self) -> str:
        return "".join(self._content)

    @property
    def tool_calls(self) -> list[_ToolCall]:
        return [
            _ToolCall(
                id=self._slots[key]["id"],
                name=self._slots[key]["name"],
                arguments=self._slots[key]["arguments"],
            )
            for key in sorted(self._slots)
        ]

    def as_message(self) -> dict[str, Any]:
        """The assistant turn to replay, with exactly the fields the API expects
        rather than a dump full of nulls."""
        payload: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        calls = self.tool_calls
        if calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in calls
            ]
        return payload


def _decode(result: str) -> Any:
    """Tool results arrive as JSON strings; give the UI real objects."""
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        return result


async def stream_chat_turn(
    user_id: str, history: list[dict[str, Any]], user_message: str
) -> AsyncIterator[dict[str, Any]]:
    """Run one user turn, yielding progress events as they happen.

    Loops until the model answers in natural language or we hit MAX_TOOL_HOPS,
    at which point we ask for a final answer with the tools withheld so the user
    always gets a reply instead of a spinning runaway.

    Yields JSON-serialisable dicts: `step` when the stage changes, a
    `tool_call`/`tool_result` pair around every MCP dispatch, and exactly one
    closing `done`. This is the only implementation of a turn — `run_chat_turn`
    drains it — so the streaming and plain endpoints cannot drift apart.
    """
    client = _client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]
    tools_used: list[str] = []

    yield {"type": "step", "stage": "connecting"}

    async with toolbox_for_user(user_id) as toolbox:
        tool_defs = await toolbox.openai_tools()

        for hop in range(MAX_TOOL_HOPS):
            yield {"type": "step", "stage": "thinking", "hop": hop}
            message = _StreamedMessage()
            try:
                started = perf_counter()
                stream = await client.chat.completions.create(
                    model=AI_MODEL,
                    messages=messages,
                    tools=tool_defs,
                    tool_choice="auto",
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue
                    message.add(delta)
                    text = getattr(delta, "content", None)
                    if text:
                        yield {"type": "delta", "text": text}
                logger.info(
                    "model round trip %.2fs (hop %s)", perf_counter() - started, hop
                )
            except OpenAIError as exc:
                logger.exception("Model call failed on hop %s", hop)
                raise ChatUnavailable(f"The co-pilot is unavailable: {exc}") from exc

            messages.append(message.as_message())
            calls = message.tool_calls

            if not calls:
                yield {
                    "type": "done",
                    "reply": message.content,
                    "tools_used": tools_used,
                }
                return

            for call in calls:
                name = call.name
                tools_used.append(name)
                try:
                    arguments = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    yield {"type": "tool_call", "name": name, "arguments": {}}
                    result = json.dumps(
                        {"error": "Arguments were not valid JSON; try again."}
                    )
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "ok": False,
                        "result": {"error": "the model sent malformed arguments"},
                        "seconds": 0.0,
                    }
                else:
                    yield {"type": "tool_call", "name": name, "arguments": arguments}
                    started = perf_counter()
                    result = await toolbox.call(name, arguments)
                    payload = _decode(result)
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "ok": not (isinstance(payload, dict) and "error" in payload),
                        "result": payload,
                        "seconds": round(perf_counter() - started, 3),
                    }

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        # Hop budget exhausted: force a closing message with no tools available.
        logger.warning("Tool hop cap (%s) reached for user %s", MAX_TOOL_HOPS, user_id)
        yield {"type": "step", "stage": "wrapping_up"}
        final = _StreamedMessage()
        try:
            stream = await client.chat.completions.create(
                model=AI_MODEL,
                messages=[
                    *messages,
                    {
                        "role": "system",
                        "content": (
                            "Tool budget exhausted. Summarise what you managed to "
                            "do for the user in plain language. Do not call tools."
                        ),
                    },
                ],
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                final.add(delta)
                text = getattr(delta, "content", None)
                if text:
                    yield {"type": "delta", "text": text}
        except OpenAIError as exc:
            logger.exception("Model wrap-up call failed")
            raise ChatUnavailable(f"The co-pilot is unavailable: {exc}") from exc

        yield {"type": "done", "reply": final.content, "tools_used": tools_used}


async def run_chat_turn(
    user_id: str, history: list[dict[str, Any]], user_message: str
) -> ChatTurn:
    """Run a turn to completion for callers that only want the answer."""
    turn = ChatTurn(reply="")
    async for event in stream_chat_turn(user_id, history, user_message):
        if event["type"] == "done":
            turn = ChatTurn(reply=event["reply"], tools_used=event["tools_used"])
    return turn
