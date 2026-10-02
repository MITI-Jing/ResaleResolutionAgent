# sap_probe.py - find usable sandbox IDs (no MCP, direct HTTP)
import asyncio, sap_backend as sb

async def main():
    d = await sb._get("/API_SALES_ORDER_SRV/A_SalesOrder",
                      {"$top": "5", "$select": "SalesOrder,SoldToParty,TotalNetAmount,CreationDate"})
    for o in d["results"]:
        print(o["SalesOrder"], o["SoldToParty"], o["TotalNetAmount"], o["CreationDate"])
    d = await sb._get("/API_BUSINESS_PARTNER/A_BusinessPartner",
                      {"$top": "5", "$select": "BusinessPartner,BusinessPartnerFullName"})
    for b in d["results"]:
        print(b["BusinessPartner"], b["BusinessPartnerFullName"])

asyncio.run(main())
    