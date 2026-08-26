"""Smoke check: spawn the MCP server and print the tool schemas the LLM will see.

    python -m scripts.check_mcp

Does not touch the database - it only performs the MCP handshake and
tools/list, so it is safe to run before Postgres exists.
"""
from __future__ import annotations

import asyncio
import json

from app.mcp_bridge import close_all_sessions, toolbox_for_user

DEMO_USER_ID = "00000000-0000-0000-0000-000000000000"


async def main() -> None:
    try:
        async with toolbox_for_user(DEMO_USER_ID) as toolbox:
            tools = await toolbox.openai_tools()
            print(f"Discovered {len(tools)} MCP tools\n")
            for tool in tools:
                print(json.dumps(tool, indent=2))
                print("-" * 60)

            names = {tool["function"]["name"] for tool in tools}
            leaky = [
                name
                for tool in tools
                for name in tool["function"]["parameters"].get("properties", {})
                if "user" in name.lower()
            ]
            print("Tool names:", sorted(names))
            print("Parameters mentioning a user id:", leaky or "none (correct)")
    finally:
        # Sessions are cached and outlive the block, so leaving them running
        # would strand a subprocess after the script exits.
        await close_all_sessions()


if __name__ == "__main__":
    asyncio.run(main())
