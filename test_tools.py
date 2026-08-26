# header and helper.
"""Tool-layer smoke test. No API calls, no model - just: does each tool run?

The escalate_to_human crash that left data/escalations.jsonl empty for three
commits was invisible to ruff and mypy. An eval harness would have reported 
it as a mediocre score, not a broken tool. Exercising each handler once is 
the only thing that catches that class of bug.

Run: python test_tools.py
"""

import asyncio
import json

import shop_agent as sa

def payload(result):
    """Unwrap a tool envelope back into the dict the model would see."""
    return json.loads(result["content"][0]["text"])

#collecting failures and show all of them in one pass.
async def main():
    checks, failures = 0, []

    def check(label, condition, detail=""):
        nonlocal checks
        checks += 1
        if not condition:
            failures.append(f"{label}: {detail}")

    with sa.sandbox() as tmp:
        r = await sa.dispatch("get_customer", {"customer_id": "C001"})
        check("get_customer", not r.get("is_error"), payload(r))
        check("get_customer name", payload(r).get("name") == "Amara Osei", payload(r))

        r = await sa.dispatch("lookup_order", {"order_id": "ORD-001"})
        o = payload(r)
        check("lookup_order", not r.get("is_error"), o)
        check("lookup_order computes days", o.get("days_since_delivery") == 5,o)
        check("lookup_order window flag", o.get("in_return_window") is True, o)

        r= await sa.dispatch("escalate_to_human",{
                "order_id": "ORD-009", "reason_code": "above_refund_authority",
                "summary": "GBP 250 Mulberry return, above auto-refund authority.",
            })
        check("escalate_to_human", not r.get("is_error"), payload(r))
        written = (tmp / "escalations.jsonl").read_text(encoding="utf-8").strip()
        check("escalate writes a ticket", written != "", "escaltions.jsonl is empty")
        if written:
            check("ticket is valid json",
                json.loads(written)["reason_code"] == "above_refund_authority")

    #guardrail table derived from process_refund tool
        cases = [
            ("clean refund",  {"order_id": "ORD-001", "amount_gbp": 35.0, "reason": "return_window"}, None),
            ("outside window", {"order_id": "ORD-004", "amount_gbp": 28.0, "reason": "return_window"}, "permission"),
            ("final sale",  {"order_id": "ORD-005", "amount_gbp": 60.0, "reason": "return_window"}, "permission"),
            ("hygiene", {"order_id": "ORD-006", "amount_gbp": 45.0, "reason": "return_window"}, "permission"),
            ("already refunded", {"order_id": "ORD-007", "amount_gbp": 38.0, "reason": "return_window"}, "permission"),
            ("above authority",  {"order_id": "ORD-009", "amount_gbp": 250.0, "reason": "return_window"},    "permission"),
            ("dispute too big",  {"order_id": "ORD-010", "amount_gbp": 45.0,  "reason": "condition_partial"},"permission"),
            ("suspended",        {"order_id": "ORD-013", "amount_gbp": 70.0,  "reason": "return_window"},    "permission"),
            ("shop fault wins",  {"order_id": "ORD-014", "amount_gbp": 40.0,  "reason": "shop_fault"},       None),
            ("unknown order",    {"order_id": "ORD-9999","amount_gbp": 10.0,  "reason": "return_window"},    "validation"),
            ("negative amount",  {"order_id": "ORD-002", "amount_gbp": -5.0,  "reason": "return_window"},    "validation"),
            ("over item price",  {"order_id": "ORD-002", "amount_gbp": 96.0,  "reason": "return_window"},    "validation"),
        ]
        for label, args, expected_category in cases:
            r = await sa.dispatch("process_refund", args)
            body = payload(r)
            if expected_category is None:
                check(f"refund/{label}", not r.get("is_error"), body)
            else:
                check(f"refund/{label} blocked",r.get("is_error"),body)
                check(f"refund/{label} category" ,
                    body.get("errorCategory") == expected_category,
                    f"got {body.get('errorCategory')!r}, want {expected_category!r}")

    #25% cap applies only to condition_partial.ORD-008 is GBP 30 -> GBP 7.50.
        r = await sa.dispatch("process_refund",
                        {"order_id": "ORD-008", "amount_gbp": 20.0, "reason": "condition_partial"})
        check("partial cap enforced", r.get("is_error"), payload(r))
        r = await sa.dispatch("process_refund",
                {"order_id": "ORD-008", "amount_gbp": 7.50, "reason": "condition_partial"})
        check("partial at cap allowed", not r.get("is_error"),payload(r))

        r = await sa.dispatch("no_such_tool", {})
        check("unknown tool", r.get("is_error") and payload(r)["errorCategory"] == "validation")

        r= await sa.dispatch("get_customer",{})
        body = payload(r)
        check("handler crash is caught", r.get("is_error"), body)
        check("crash is non-retryable", body.get("isRetryable") is False, body)

    check("sandbox restored DB", "shop-sandbox-" not in sa.DB, sa.DB)
    live = sa.q("SELECT status, refund_count FROM orders WHERE order_id = 'ORD-001'")[0]
    check("real ORD-001 untouched",
              live["status"] == "delivered" and live["refund_count"] == 0, live)

    print(f"{checks - len(failures)}/{checks} checks passed")
    for f in failures:
            print("  FAIL",f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
    
        


                