"""Drop-in replacement for shop_agent.dispatch that goes over MCP.

A session per call: _ tens of ms locally, stateless, and the extra latency
up honestly in wall_ms.
"""

import os

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("SHOP_MCP_URL", "http://127.0.0.1:8100/mcp")

async def dispatch(name: str, args: dict) -> dict:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(name, args)
            text = res.content[0].text if res.content else ""
            return {"content": [{"type": "text", "text": text}],
                    "is_error": bool(res.isError)}