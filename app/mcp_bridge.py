"""Bridge between the chat handler and the Stash MCP server.

The chat handler never imports a repository or opens a database cursor. It asks
this bridge for tool definitions, hands them to the LLM, and posts the model's
tool calls back here to be executed over MCP's stdio JSON-RPC transport.

Scoping is enforced by construction: the MCP subprocess is launched with
STASH_USER_ID baked into its environment, and none of the tool schemas expose a
user id, so the model has no way to address another user's data.

Sessions are cached per user and reused across turns. Starting the subprocess
costs ~1s locally and ~7s on a throttled instance, which dominated chat latency
when every message paid it. The scoping guarantee is unaffected: the cache is
keyed by user id, so a process is only ever reused for the user it was spawned
for, and it still cannot name anyone else.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment

from app.config import (
    DATABASE_URL,
    MCP_MAX_LIVE_SESSIONS,
    MCP_SESSION_IDLE_SECONDS,
    MCP_SESSION_SWEEP_SECONDS,
)

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# How long a single tool call may take before we give up on the subprocess.
TOOL_TIMEOUT_SECONDS = 20.0


class MCPToolbox:
    """Thin adapter exposing an MCP session in the shape the LLM expects."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._tools: list[Any] | None = None
        # Flipped when the transport misbehaves, so a session that died between
        # turns is replaced instead of failing every future call.
        self.broken = False

    async def openai_tools(self) -> list[dict[str, Any]]:
        """Translate MCP tool definitions into OpenAI function-tool schemas."""
        if self._tools is None:
            self._tools = (await self._session.list_tools()).tools
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in self._tools
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one tool call and return a JSON string for the model.

        Tool failures are returned as content rather than raised, so the model
        can apologise or retry instead of the whole turn 500-ing.
        """
        try:
            result = await self._session.call_tool(
                name, arguments, read_timeout_seconds=TOOL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.exception("MCP tool %s failed", name)
            # A tool that errors reports it through result.is_error, so an
            # exception here means the transport itself is suspect.
            self.broken = True
            return json.dumps({"error": f"Tool {name!r} failed: {exc}"})

        payload = self._extract(result)
        if result.is_error:
            logger.warning("MCP tool %s reported an error: %s", name, payload)
            return json.dumps({"error": payload})
        return json.dumps(payload, default=str)

    @staticmethod
    def _extract(result: Any) -> Any:
        if result.structured_content is not None:
            return result.structured_content
        texts = [
            block.text for block in (result.content or []) if getattr(block, "text", None)
        ]
        return "\n".join(texts) if texts else ""


@dataclass
class _LiveSession:
    """A running MCP subprocess and the bookkeeping to share and retire it."""

    user_id: str
    ready: asyncio.Future  # resolves to MCPToolbox, or raises if the spawn failed
    closer: asyncio.Event  # set to ask the owner task to shut the session down
    lock: asyncio.Lock  # one chat turn at a time per session
    last_used: float
    task: asyncio.Task | None = field(default=None)


_sessions: dict[str, _LiveSession] = {}
# Guards _sessions only. Never held across a spawn, so slow starts for one user
# do not queue behind another's.
_registry_lock = asyncio.Lock()
_reaper: asyncio.Task | None = None


def _server_params(user_id: str) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_server.server"],
        cwd=str(PROJECT_ROOT),
        # Least privilege: the bookmark server gets the database and the user it
        # serves, not the app's LLM or email credentials.
        env={
            **get_default_environment(),
            "DATABASE_URL": DATABASE_URL,
            "STASH_USER_ID": str(user_id),
        },
    )


def _forget(entry: _LiveSession) -> None:
    """Unregister, but only if this entry is still the current one — a session
    ending after it was replaced must not evict its successor."""
    if _sessions.get(entry.user_id) is entry:
        del _sessions[entry.user_id]


def _retire(entry: _LiveSession) -> None:
    """Ask the owner task to tear the session down. Does not wait for it."""
    entry.closer.set()
    _forget(entry)


def _is_stale(entry: _LiveSession) -> bool:
    if entry.task is not None and entry.task.done():
        return True
    if entry.ready.done() and not entry.ready.cancelled():
        if entry.ready.exception() is not None:
            return True
        return entry.ready.result().broken
    return False


def _start_session(user_id: str) -> _LiveSession:
    """Register a session and launch its owner task.

    Deliberately synchronous: the caller holds the registry lock, and this must
    not await. The subprocess starts in the background and callers wait on
    `entry.ready`.
    """
    loop = asyncio.get_running_loop()
    entry = _LiveSession(
        user_id=user_id,
        ready=loop.create_future(),
        closer=asyncio.Event(),
        lock=asyncio.Lock(),
        last_used=monotonic(),
    )

    async def own() -> None:
        started = perf_counter()
        try:
            async with stdio_client(_server_params(user_id)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    logger.info(
                        "MCP session ready in %.2fs (user %s)",
                        perf_counter() - started,
                        user_id,
                    )
                    if not entry.ready.done():
                        entry.ready.set_result(MCPToolbox(session))
                    # anyio requires the task that entered these context
                    # managers to be the one that exits them, so the session
                    # lives inside this task until it is retired.
                    await entry.closer.wait()
        except Exception as exc:
            if not entry.ready.done():
                entry.ready.set_exception(exc)
            else:
                logger.warning("MCP session for %s ended early: %s", user_id, exc)
        finally:
            _forget(entry)

    entry.task = loop.create_task(own(), name=f"mcp-session-{user_id[:8]}")
    return entry


def _evict_over_capacity() -> None:
    """Retire least-recently-used idle sessions until back under the cap.

    Each cached session is an idle interpreter, so the limit is memory rather
    than database connections.
    """
    while len(_sessions) > MCP_MAX_LIVE_SESSIONS:
        idle = [entry for entry in _sessions.values() if not entry.lock.locked()]
        if not idle:
            return  # everything is mid-turn; the reaper will catch up later
        victim = min(idle, key=lambda entry: entry.last_used)
        logger.info("Evicting MCP session for %s to stay under cap", victim.user_id)
        _retire(victim)


async def _reap_idle() -> None:
    while True:
        await asyncio.sleep(MCP_SESSION_SWEEP_SECONDS)
        cutoff = monotonic() - MCP_SESSION_IDLE_SECONDS
        async with _registry_lock:
            for entry in list(_sessions.values()):
                if not entry.lock.locked() and entry.last_used < cutoff:
                    logger.info("Evicting idle MCP session for %s", entry.user_id)
                    _retire(entry)


def _ensure_reaper() -> None:
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap_idle(), name="mcp-session-reaper")


async def _acquire(user_id: str) -> tuple[_LiveSession, MCPToolbox]:
    async with _registry_lock:
        entry = _sessions.get(user_id)
        if entry is not None and _is_stale(entry):
            _retire(entry)
            entry = None
        if entry is None:
            entry = _start_session(user_id)
            _sessions[user_id] = entry
            _ensure_reaper()
            _evict_over_capacity()

    # Outside the registry lock: concurrent turns for the same user await the
    # same future, and other users are free to start their own sessions.
    return entry, await entry.ready


@asynccontextmanager
async def toolbox_for_user(user_id: str) -> AsyncIterator[MCPToolbox]:
    """Yield a toolbox backed by this user's MCP server, starting one if needed.

    The session outlives the block and is reused by the user's next turn, so
    only their first message pays the startup cost.
    """
    entry, toolbox = await _acquire(str(user_id))
    # One turn at a time per session: MCP is request/response over a single
    # pipe, and serialising is cheaper than reasoning about interleaved calls.
    async with entry.lock:
        entry.last_used = monotonic()
        try:
            yield toolbox
        finally:
            entry.last_used = monotonic()


async def close_all_sessions() -> None:
    """Shut every session down. Called from the app's lifespan on shutdown."""
    global _reaper
    if _reaper is not None:
        _reaper.cancel()
        _reaper = None

    async with _registry_lock:
        entries = list(_sessions.values())
        for entry in entries:
            _retire(entry)

    tasks = [entry.task for entry in entries if entry.task is not None]
    if tasks:
        await asyncio.wait(tasks, timeout=10)
