"""Break a chat turn into phases and time each one.

    python -m scripts.profile_chat                    # local, per-phase
    python -m scripts.profile_chat https://host       # deployed, end-to-end

Locally this wraps the real pipeline rather than reimplementing it, so the
numbers describe the code that actually runs: MCP subprocess spawn, tool-schema
listing, every LLM round trip, and every tool execution. Needs DATABASE_URL and
AI_API_KEY, and creates a throwaway user that it deletes afterwards.

Given a URL it measures the deployed service over HTTP instead. A turn that
needs no tools costs one model round trip; a turn that uses a tool costs two
plus the tool itself, so comparing the two decomposes the latency without
instrumenting the server.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import statistics
import sys
from collections import defaultdict
from contextlib import asynccontextmanager
from time import perf_counter

import httpx
from dotenv import load_dotenv

load_dotenv()

from app import chat as chat_module  # noqa: E402
from app import db, mcp_bridge, repository  # noqa: E402
from app.security import hash_password  # noqa: E402

# label -> list of durations, in call order
timings: dict[str, list[float]] = defaultdict(list)
order: list[tuple[str, float]] = []


def record(label: str, seconds: float) -> None:
    timings[label].append(seconds)
    order.append((label, seconds))


def install_probes() -> None:
    """Wrap the real functions in timers, leaving behaviour untouched."""
    real_toolbox = chat_module.toolbox_for_user

    @asynccontextmanager
    async def timed_toolbox(user_id: str):
        started = perf_counter()
        async with real_toolbox(user_id) as toolbox:
            # Near-zero once a session is cached; the first turn pays the spawn.
            record("mcp session acquire", perf_counter() - started)
            yield toolbox

    chat_module.toolbox_for_user = timed_toolbox

    real_list = mcp_bridge.MCPToolbox.openai_tools

    async def timed_list(self):
        started = perf_counter()
        result = await real_list(self)
        record("mcp list_tools", perf_counter() - started)
        return result

    mcp_bridge.MCPToolbox.openai_tools = timed_list

    real_call = mcp_bridge.MCPToolbox.call

    async def timed_call(self, name, arguments):
        started = perf_counter()
        result = await real_call(self, name, arguments)
        record(f"tool: {name}", perf_counter() - started)
        return result

    mcp_bridge.MCPToolbox.call = timed_call

    real_client = chat_module._client

    def timed_client():
        client = real_client()
        create = client.chat.completions.create

        async def wrapped(*args, **kwargs):
            started = perf_counter()
            response = await create(*args, **kwargs)
            record("llm round trip", perf_counter() - started)
            return response

        client.chat.completions.create = wrapped
        return client

    chat_module._client = timed_client


async def profile_turn(user_id: str, message: str) -> None:
    order.clear()
    print(f"\n--- {message!r}")
    started = perf_counter()
    turn = await chat_module.run_chat_turn(
        user_id=user_id, history=[], user_message=message
    )
    total = perf_counter() - started

    for label, seconds in order:
        share = 100 * seconds / total if total else 0
        bar = "#" * max(1, round(share / 3))
        print(f"  {seconds:6.2f}s  {share:5.1f}%  {bar:<34} {label}")
    print(f"  {total:6.2f}s  TOTAL   tools={turn.tools_used}")


def profile_live(base: str, repeats: int = 3) -> int:
    """Time real chat requests against a deployment."""
    base = base.rstrip("/")
    timeout = httpx.Timeout(300.0, connect=60.0)
    email = f"perf-{secrets.token_hex(5)}@example.com"
    password = secrets.token_hex(12)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        print(f"Waking {base} ...")
        started = perf_counter()
        client.get(f"{base}/healthz")
        print(f"  cold-start / health: {perf_counter() - started:.2f}s")

        started = perf_counter()
        client.post(f"{base}/signup", data={"email": email, "password": password})
        print(f"  signup (bcrypt + insert): {perf_counter() - started:.2f}s")

        scenarios = [
            ("no tools    ", "Hello, what can you do?"),
            ("1 tool (write)", f"Save example-{secrets.token_hex(3)}.com, tag it reading."),
            ("1 tool (read) ", "What have I saved?"),
        ]
        summary: dict[str, list[float]] = {}
        for label, message in scenarios:
            samples = []
            for _ in range(repeats):
                started = perf_counter()
                response = client.post(
                    f"{base}/api/chat", json={"message": message}
                )
                elapsed = perf_counter() - started
                samples.append(elapsed)
                tools = (
                    response.json().get("tools_used", [])
                    if response.status_code == 200
                    else f"HTTP {response.status_code}"
                )
            summary[label] = samples
            median = statistics.median(samples)
            shown = ", ".join(f"{s:.2f}" for s in samples)
            print(f"  {label}  median {median:5.2f}s   [{shown}]   tools={tools}")

    no_tools = statistics.median(summary["no tools    "])
    with_tool = statistics.median(summary["1 tool (read) "])
    print("\n=== what the numbers imply ===")
    print(f"  a turn with no tool call:   {no_tools:5.2f}s")
    print(f"  a turn with one tool call:  {with_tool:5.2f}s")
    print(f"  cost of the extra hop:      {with_tool - no_tools:5.2f}s")
    print(
        "  the no-tool turn is still one MCP spawn + one model round trip, so\n"
        "  that figure is the floor every message pays."
    )
    return 0


async def main() -> int:
    if len(sys.argv) > 1:
        return profile_live(sys.argv[1])

    if not os.environ.get("AI_API_KEY", "").strip():
        print("AI_API_KEY is not set.", file=sys.stderr)
        return 1

    install_probes()
    db.open_pool()
    email = f"profile-{secrets.token_hex(4)}@example.com"
    user = repository.create_user(email, hash_password(secrets.token_hex(12)))
    user_id = str(user["id"])

    try:
        # A no-tool turn isolates pure model latency; the others add one and two
        # tool hops on top of it.
        await profile_turn(user_id, "Hello, what can you do?")
        await profile_turn(
            user_id, "Save example.com, title it Example Domain, tag it reading."
        )
        await profile_turn(user_id, "What have I saved?")

        print("\n=== totals by phase across all three turns ===")
        grand = sum(sum(v) for v in timings.values())
        for label, values in sorted(
            timings.items(), key=lambda kv: sum(kv[1]), reverse=True
        ):
            total = sum(values)
            print(
                f"  {total:6.2f}s  {100 * total / grand:5.1f}%  "
                f"n={len(values):<3} avg={total / len(values):5.2f}s  {label}"
            )
    finally:
        await mcp_bridge.close_all_sessions()
        repository.delete_users([user_id])
        db.close_pool()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
