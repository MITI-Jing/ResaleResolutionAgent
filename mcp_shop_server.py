"""Standalone MCP server for the shop tools - streamable HTTP on :8100.
    python mcp_shop_server.py                      #sqlite backend(data/shop.db)
    SHOP_BACKEND=sap python mcp_shop_server.py     #SAP sandbox reads (step 4b)

Same tool names, same envelope. The error flag now travels inside the JSON body
rather than as MCP isError - the model reads the body either way.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP

import shop_agent as sa

mcp = FastMCP("shop", port=8100)
DESC = {t["name"]: t["description"] for t in sa.SHOP_TOOLS}
BACKEND = os.environ.get("SHOP_BACKEND", "sqlite")

async def _run(name: str, args: dict) -> str:
    if BACKEND == "sap":
        import sap_backend
        env = await sap_backend.dispatch(name, args)
    else:
        env = await sa.dispatch(name, args)
    return env["content"][0]["text"]

@mcp.tool(description=DESC["get_customer"])
async def get_customer(customer_id: str) -> str:
    return await _run("get_customer", {"customer_id": customer_id})

@mcp.tool(description=DESC["lookup_order"])
async def lookup_order(order_id: str) -> str:
    return await _run("lookup_order", {"order_id": order_id})

@mcp.tool(description=DESC["process_refund"])
async def process_refund(order_id: str, amount_gbp: float,
                         reason: Literal["return_window", "shop_fault", "condition_partial"]) -> str:
    return await _run("process_refund",
                      {"order_id": order_id, "amount_gbp": amount_gbp, "reason": reason})

@mcp.tool(description=DESC["escalate_to_human"])
async def escalate_to_human(reason_code: str, summary: str, order_id: str | None = None) -> str:
    return await _run("escalate_to_human",
                      {"reason_code": reason_code, "summary": summary, "order_id": order_id})

# Dev-only: eval re-points the server at each case's sandbox copy. The model can
# never call this - the agent's tool list is LC_TOOLS, which doesn't include it.
if os.environ.get("SHOP_DEV") == "1":
    @mcp.tool(description="dev only")
    async def use_fixture(db_path: str, escalations_path: str) -> str:
        sa.DB = db_path
        sa.ESCALATIONS = Path(escalations_path)
        return "ok"


if __name__ == "__main__":
    mcp.run(transport="streamable-http") #http://127.0.0.1:8100/mcp
