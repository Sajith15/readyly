"""The AI co-pilot: an LLM turn that reaches bookmarks only through MCP.

There is no database access in this module. Every read or write happens because
the model emitted a tool call that we dispatched over the MCP bridge.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from app.config import MAX_TOOL_HOPS, OPENAI_API_KEY, OPENAI_MODEL
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
    if not OPENAI_API_KEY:
        raise ChatUnavailable(
            "The co-pilot is not configured: OPENAI_API_KEY is missing."
        )
    return AsyncOpenAI(api_key=OPENAI_API_KEY)


def _assistant_message(message: Any) -> dict[str, Any]:
    """Rebuild the assistant turn explicitly, so we send back exactly the fields
    the API expects rather than a dump full of nulls."""
    payload: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.function.name,
                    "arguments": call.function.arguments,
                },
            }
            for call in message.tool_calls
        ]
    return payload


async def run_chat_turn(
    user_id: str, history: list[dict[str, Any]], user_message: str
) -> ChatTurn:
    """Run one user turn to completion, dispatching tool calls through MCP.

    Loops until the model answers in natural language or we hit MAX_TOOL_HOPS,
    at which point we ask for a final answer with the tools withheld so the user
    always gets a reply instead of a spinning runaway.
    """
    client = _client()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]
    tools_used: list[str] = []

    async with toolbox_for_user(user_id) as toolbox:
        tool_defs = await toolbox.openai_tools()

        for hop in range(MAX_TOOL_HOPS):
            try:
                response = await client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=messages,
                    tools=tool_defs,
                    tool_choice="auto",
                )
            except OpenAIError as exc:
                logger.exception("OpenAI call failed on hop %s", hop)
                raise ChatUnavailable(f"The co-pilot is unavailable: {exc}") from exc

            message = response.choices[0].message
            messages.append(_assistant_message(message))

            if not message.tool_calls:
                return ChatTurn(reply=message.content or "", tools_used=tools_used)

            for call in message.tool_calls:
                name = call.function.name
                tools_used.append(name)
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    result = json.dumps(
                        {"error": "Arguments were not valid JSON; try again."}
                    )
                else:
                    result = await toolbox.call(name, arguments)

                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": result}
                )

        # Hop budget exhausted: force a closing message with no tools available.
        logger.warning("Tool hop cap (%s) reached for user %s", MAX_TOOL_HOPS, user_id)
        try:
            final = await client.chat.completions.create(
                model=OPENAI_MODEL,
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
            )
        except OpenAIError as exc:
            logger.exception("OpenAI wrap-up call failed")
            raise ChatUnavailable(f"The co-pilot is unavailable: {exc}") from exc

        return ChatTurn(
            reply=final.choices[0].message.content or "", tools_used=tools_used
        )
