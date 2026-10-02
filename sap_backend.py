"""SAP sandbox backend: live S/4HANA Cloud reads, local ledger for writes.

Same envelope as shop_agent's tools (reuses ok/err), same guardrail philosophy:
policy stays in the tool layer. process_refund would POST a Credit Memo Request
(API_CREDIT_MEMO_REQUEST_SRV) against a real tenant; the public sandbox rejects
writes, so it records to data/sap_ledger.jsonl instead and duplicate checks
read that ledger back.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

import shop_agent as sa
from shop_agent import ok, err

BASE = "https://sandbox.api.sap.com/s4hanacloud/sap/opu/odata/sap"
LEDGER = Path(__file__).resolve().parent / "data" / "sap_ledger.jsonl"

def _odate(s: str | None) -> date | None:
    """OData v2 dates arrive as /Date(1492041600000)/."""
    m = re.search(R"\d+", s or "")
    return datetime.fromtimestamp(int(m.group()) / 1000, tz=timezone.utc).date() if m else None

async def _get(path: str, params: dict | None = None) -> dict:
    headers = {"APIKey": os.environ["SAP_API_KEY"], "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{BASE}{path}", params=params or {}, headers=headers)
        r.raise_for_status()
        return r.json()["d"]

def _ledger() -> list[dict]:
    if not LEDGER.exists():
        return []
    return [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]

async def get_customer(args: dict) -> dict:
    try:
        d = await _get(f"/API_BUSINESS_PARTNER/A_BusinessPartner('{args['customer_id']})")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return err("validation", f"No customer {args['customer_id']}")
        raise
    return ok({"customer_id": d["BusinessPartner"],
               "name": d.get("BusinessPartnerFullName") or d.get("BusinessPartnerName"),
               "account_status": "suspended" if d.get("BusinessPartnerIsBlocked") else "active",
               "joined_date": str(_odate(d.get("CreationDate")))})
    
async def lookup_order(args: dict) -> dict:
    try:
        d = await _get(f"/API_SALES_ORDER_SRV/A_SalesOrder('{args['order_id']}')",
                       {"$expand": "to_Item"})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return err("validation",
                       f"No order {args['order_id']}. Ask the customer to confirm the ID.")
        raise
    items = d.get("to_Item", {}).get("results",[])
    first = items[0] if items else {}
    delivered = _odate(d.get("RequestedDeliveryDate")) or _odate(d.get("CreationDate"))
    days = (date.today() - delivered).days if delivered else None
    refunded = any(r["order_id"] == args["order_id"] for r in _ledger())
    return ok({"order_id": d["SalesOrder"], "customer_id": d.get("SoldToParty"),
               "order_date": str(delivered), "status": d.get("OverallSDProcessStatus"),
               "days_since_delivery": days,
               "in_return_window": days is not None and days <= sa.POLICY["return_window_days"],
               "already_refund": refunded, "refund_count": int(refunded),
               "brand": first.get("Material"), "category": first.get("SalesOrderItemText"),
               "condition": "n/a",
               "price_gbp": float(d.get("TotalNetAmount") or 0),
               "final_sale": False})  # no S/4 analog; policy branch stays dormant on sap

async def process_refund(args: dict) -> dict:
    if any(r["order_id"] == args["order_id"] for r in _ledger()):
        return err("permission", "Order already refunded. Explain this to the customer.")
    if args["amount_gbp"] > sa.POLICY["auto_refund_limit_gbp"]:
        return err("permission",
                   f"GBP{args['amount_gbp']:.2f} exceeds the GBP{sa.POLICY['auto_refund_limit_gbp']}"
                   "auto-refund authority - escalate_to_human.")
    if args["amount_gbp"] <= 0:
        return err("validation", "Refund amount must be greater than zero.", retryable=True)
    LEDGER.parent.mkdir(exist_ok=True)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({"order_id": args["order_id"], "amount_gbp": args["amount_gbp"],
                            "reason": args["reason"], "at": date.today().isoformat()}) + "\n")
    return ok({"refunded": True, "order_id": args["order_id"],
               "amount_gbp": args["amount_gbp"], "new_status": "refunded"})

HANDLERS = {"get_customer": get_customer, "lookup_order": lookup_order,
            "process_refund": process_refund,
            "escalate_to_human": sa.escalate_to_human.handler}  #tickets stay local

async def dispatch(name: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return err("validation", f"Unknown tool {name!r}.")
    try:
        return await handler(args)
    except httpx.HTTPError as e:
        return err("transient", f"SAP sandbox: {type(e).__name__}: {e}", retryable=True)

    



    