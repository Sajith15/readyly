"""Exercise the chat tool-call loop against a stubbed LLM.

The model is the one component we cannot assert on cheaply or deterministically,
so this replaces it with a scripted stand-in and checks the parts we own: that
tool calls are dispatched through MCP, that results are fed back in the shape
the API expects, and that the hop cap ends a runaway with a real answer.

    python -m scripts.chat_loop_test

Needs DATABASE_URL and an initialised schema, but no AI_API_KEY.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from types import SimpleNamespace

from app import chat, db, repository
from app.mcp_bridge import close_all_sessions
from app.security import hash_password

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" - {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(label)


def tool_call(call_id: str, name: str, **arguments) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def reply(content: str | None, tool_calls: list | None = None) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ScriptedLLM:
    """Stands in for AsyncOpenAI, returning a canned response per turn."""

    def __init__(self, script: list, wrap_up: SimpleNamespace | None = None) -> None:
        self.script = list(script)
        self.wrap_up = wrap_up
        self.received: list[list[dict]] = []
        self.tool_defs_seen: list | None = None
        self.calls_without_tools = 0

    async def _create(self, *, model, messages, tools=None, tool_choice=None):
        # Snapshot: the loop keeps appending to the same list, so storing the
        # reference would show every call the final state.
        self.received.append(list(messages))
        if tools is None:
            # The hop-cap wrap-up call, which deliberately offers no tools.
            self.calls_without_tools += 1
            return self.wrap_up
        self.tool_defs_seen = tools
        # A single-entry script repeats, which is how the runaway case is built.
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))


async def run_with(stub: ScriptedLLM, user_id: str, message: str) -> chat.ChatTurn:
    original = chat._client
    chat._client = lambda: stub
    try:
        return await chat.run_chat_turn(user_id, [], message)
    finally:
        chat._client = original


async def test_tool_calls_are_dispatched_through_mcp(user_id: str) -> None:
    print("\nTool calls reach the database through MCP")

    stub = ScriptedLLM([
        reply(None, [tool_call("call_1", "add_bookmark",
                               url="https://example.com",
                               title="Example",
                               tags=["reading"])]),
        reply(None, [tool_call("call_2", "search_bookmarks", query="example")]),
        reply("Saved it and found it again."),
    ])

    turn = await run_with(stub, user_id, "Save https://example.com under tag reading")

    check("the loop returns the model's final prose",
          turn.reply == "Saved it and found it again.", turn.reply)
    check("both tool calls were dispatched",
          turn.tools_used == ["add_bookmark", "search_bookmarks"], str(turn.tools_used))

    names = {tool["function"]["name"] for tool in (stub.tool_defs_seen or [])}
    check("MCP tool definitions were offered to the model",
          names == {"add_bookmark", "list_bookmarks", "search_bookmarks",
                    "delete_bookmark"}, str(names))

    final_messages = stub.received[-1]
    tool_results = [m for m in final_messages if m.get("role") == "tool"]
    check("tool results are fed back with their call ids",
          [m["tool_call_id"] for m in tool_results] == ["call_1", "call_2"],
          str(tool_results))

    assistant_turns = [m for m in final_messages if m.get("role") == "assistant"]
    check("the assistant's tool_calls are replayed in full",
          all("tool_calls" in m for m in assistant_turns), str(assistant_turns))

    search_result = json.loads(tool_results[1]["content"])
    check("the search result came back through MCP with real data",
          search_result.get("count") == 1, str(search_result))

    # Prove the write actually landed, using a separate MCP session.
    from app.mcp_bridge import toolbox_for_user

    async with toolbox_for_user(user_id) as toolbox:
        listed = json.loads(await toolbox.call("list_bookmarks", {}))
    check("the bookmark was really persisted", listed["count"] == 1, str(listed))
    check("it kept the tag the model supplied",
          listed["bookmarks"][0]["tags"] == ["reading"], str(listed))


async def test_unknown_tool_is_reported_not_raised(user_id: str) -> None:
    print("\nA hallucinated tool name is handled")

    stub = ScriptedLLM([
        reply(None, [tool_call("call_1", "drop_everything", confirm=True)]),
        reply("I could not do that."),
    ])

    turn = await run_with(stub, user_id, "do something impossible")
    check("the turn still completes", turn.reply == "I could not do that.")

    tool_results = [m for m in stub.received[-1] if m.get("role") == "tool"]
    payload = json.loads(tool_results[0]["content"])
    check("the failure is handed back to the model as content",
          "error" in payload, str(payload))


async def test_hop_cap_ends_a_runaway(user_id: str) -> None:
    print("\nThe hop cap stops a runaway")

    # A single-entry script repeats forever: the model never stops asking for
    # another tool call.
    stub = ScriptedLLM(
        [reply(None, [tool_call("call_x", "list_bookmarks")])],
        wrap_up=reply("I ran out of steps, but here is where I got to."),
    )

    turn = await run_with(stub, user_id, "loop forever")

    check("the turn still returns a natural-language reply",
          turn.reply == "I ran out of steps, but here is where I got to.", turn.reply)
    check("tool calls stopped at the cap",
          len(turn.tools_used) == chat.MAX_TOOL_HOPS, str(len(turn.tools_used)))
    check("the closing call withheld the tools", stub.calls_without_tools == 1,
          str(stub.calls_without_tools))


async def main() -> int:
    db.open_pool()
    user = repository.create_user(
        f"chatloop-{uuid.uuid4().hex[:8]}@example.test", hash_password("some password")
    )
    user_id = str(user["id"])

    try:
        await test_tool_calls_are_dispatched_through_mcp(user_id)
        await test_unknown_tool_is_reported_not_raised(user_id)
        await test_hop_cap_ends_a_runaway(user_id)
    finally:
        await close_all_sessions()
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
