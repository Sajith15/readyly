"""Bridge between the chat handler and the Stash MCP server.

The chat handler never imports a repository or opens a database cursor. It asks
this bridge for tool definitions, hands them to the LLM, and posts the model's
tool calls back here to be executed over MCP's stdio JSON-RPC transport.

Scoping is enforced by construction: the MCP subprocess is launched with
STASH_USER_ID baked into its environment, and none of the tool schemas expose a
user id, so the model has no way to address another user's data.
"""
from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment

from app.config import DATABASE_URL

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# How long a single tool call may take before we give up on the subprocess.
TOOL_TIMEOUT_SECONDS = 20.0


class MCPToolbox:
    """Thin adapter exposing an MCP session in the shape the LLM expects."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self._tools: list[Any] | None = None

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


@asynccontextmanager
async def toolbox_for_user(user_id: str) -> AsyncIterator[MCPToolbox]:
    """Spawn a user-scoped MCP server and yield a connected toolbox.

    A fresh subprocess per chat turn keeps the scoping guarantee trivially
    auditable. See the README for why this is a deliberate trade-off over
    pooling long-lived sessions.
    """
    params = StdioServerParameters(
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
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPToolbox(session)
