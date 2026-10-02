# mcp_smoke.py - client-> :8100 -> SAP sandbox

import asyncio, mcp_dispatch

async def main():
    for name, args in [("lookup_order", {"order_id": "PASTE_SalesOrder"}),
                       ("get_customer", {"customer_id": "PASTE_SoldToParty"})]:
        env = await mcp_dispatch.dispatch(name, args)
        print(name, "->", env["content"][0]["text"][:300])

asyncio.run(main())