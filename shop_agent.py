import asyncio
import sqlite3
import json
from datetime import date
from typing import Any
from dotenv import load_dotenv
import hashlib
import re
from pathlib import Path
import shutil
import tempfile
from contextlib import contextmanager
import sys
import threading


import anthropic
from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage,
    ToolUseBlock, query, tool, create_sdk_mcp_server, ToolAnnotations,
)


load_dotenv()

#1. Paths, policy

DATA = Path(__file__).resolve().parent / "data"
DB = str(DATA / "shop.db")
ESCALATIONS = DATA / "escalations.jsonl"
POLICY = json.loads((DATA / "policy.json").read_text(encoding="utf-8"))

TODAY = date.fromisoformat(POLICY["dataset_today"])

MODEL = "claude-opus-5"

# 2. sandbox() copies shop.db to a temp directory and points the module at the copy.
@contextmanager
def sandbox(pristine: str | Path | None = None): 
    """Run against a throwaway copy of the dataset.
    
    process_refund and escalate_to_human both write. Without this the fixture
    and the system under test are the same file: one case mutates the rows 
    the next case is scored against.
    """
    global DB, ESCALATIONS

    src = Path(pristine) if pristine else DATA / "shop.db"
    original = (DB, ESCALATIONS)
    tmp = Path(tempfile.mkdtemp(prefix="shop-sandbox-"))

    try:
        shutil.copy(src, tmp / "shop.db")
        (tmp / "escalations.jsonl").write_text("", encoding="utf-8")
        DB, ESCALATIONS = str(tmp / "shop.db"), tmp / "escalations.jsonl"
        yield tmp
    finally:
        DB, ESCALATIONS = original
        shutil.rmtree(tmp, ignore_errors=True)


# 3. convert the sql database to list[dict]

def q(sql, params=()):
    con = sqlite3.connect(DB); #creates a Connection object, stores it in'con', list[tuple],poisitional only.
    con.row_factory = sqlite3.Row #configure object 'con', makes each row a ROW,list[sqlite3.Row],print as opaque objects.
    try:   
        rows = [dict(r) for r in con.execute(sql, params)]  #q returns list[dict],every value is str.
        con.commit()
        return rows
    finally: 
        con.close()


# convert the list[dict] to json file
def ok(playload):
    return {"content": [{"type": "text", "text": json.dumps(playload)}]} 

#4. error message

def err(category, message, retryable=False):
    """errorCategory: transient | validation | permission"""
    # Every tool message funnels through here, so this one assert catches the
    # missing-f-prefix bug class across the whole tool layer.
    assert "{" not in message, f"unrendered brace in error message: {message!r}"
    return {
        "content": [{"type": "text", "text": json.dumps({
            "error": True, "errorCategory": category,
            "isRetryable":retryable, "message": message,
        })}],
        "is_error": True,
    }


# 5. Tools 
# creating get_customer mcp tool
@tool(
    "get_customer",
    "Fetch a customer record by ID. Return name,email,account status "
    "('active' or 'suspended'), and joined_date. Use for account-level context,"
    "such as confirming the account is in good standing before a refund."
    "Does NOT return orders - use lookup_order for anything order-specific.",
    {"customer_id": str},
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def get_customer(args: dict[str, Any]) -> dict[str, Any]:
    rows = q("SELECT * FROM customers WHERE customer_id = ?",(args["customer_id"],))
    if not rows:
        return err("validation", f"No customer {args['customer_id']}")

    return ok(rows[0])



# lookup_order mcp tool
@tool(
    "lookup_order",
    "Look for customer order by ID. Return order_date,delivery_date,status,"
    "days_since_delivery, in_return_window,already_refund, customer_id and"
    "item details(brand, category, condition, price_gbp, final_sale)"
    "Call this before deciding on any refund, or exchange - it is "
    "the single source of truth for eligibility facts."
    "Do NOT process anything",
    {"order_id": str},
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def lookup_order(args: dict[str, Any]) -> dict[str, Any]:
    rows = q("""
        SELECT o.order_id, o.customer_id, o.order_date, o.delivery_date,
                o.status, o.refund_count,
                i.brand, i.category, i.condition, i.price_gbp, i.final_sale
        FROM orders o
        JOIN items i ON i.item_id = o.item_id
        WHERE o.order_id = ?
     """, (args["order_id"],))

    if not rows:
        return err("validation", 
                   f"No order {args['order_id']}. Ask the customer to confirm the ID.")

    o = rows[0] # take the first row that has order details
    delivered = date.fromisoformat(o["delivery_date"]) if o["delivery_date"] else None #SQLite has no date type,to parse it into a real date object.
    days = (TODAY - delivered).days if delivered else None

    o["days_since_delivery"] = days
    o["in_return_window"] = days is not None and days <= POLICY["return_window_days"]
    o["already_refund"] = o["refund_count"] >0 or o["status"] in ("refunded","partially_refunded")
    o["final_sale"] = bool(o["final_sale"])
    return ok(o)




#process refund tool
@tool(
    "process_refund",
    "Issue a refund against an order.Use ONLY when lookup_order confirms the order is "
    f"refundable and the amount is below the GBP {POLICY['auto_refund_limit_gbp']} "
    "auto-refund limit. Pass reason='condition_partial' for a goodwill partial refund, and "
    "reason='shop_fault' when the shop is at fault(wrong item sent, damaged in transit, "
    "misdescribed) - shop_fault is the only reason that overrides the return window and final sale."
    f"No refund for hygiene products under {','.join(POLICY['hygiene_excluded_categories'])}."
    "Do NOT use it for authenticity claims, delivery disputes, suspended accounts,or "
    "condition disputes above the dispute limit - those require escalate_to_human."
    "Rejects out-of-authroity requests; a rejection means escalate, not retry.",
    {
        "type": "object",
        "properties": {
            "order_id":  {"type": "string"},
            "amount_gbp": {"type": "number", "description": "refund amount in GBP."},
            "reason": {"type": "string",
                       "enum": ["return_window", "shop_fault", "condition_partial"]},
        },
        "required": ["order_id", "amount_gbp", "reason"],
    }
)

async def process_refund(args: dict[str, Any]) -> dict[str,Any]:
    rows = q("""
        SELECT o.*, c.account_status, i.price_gbp, i.final_sale,i.category
        FROM orders o
        JOIN customers c ON c.customer_id = o.customer_id
        JOIN items i on i.item_id = o.item_id
        WHERE o.order_id = ?""", (args["order_id"],))

    if not rows:
        return err("validation", f"No order {args['order_id']}.")

    o = rows[0]

    
    if o["account_status"] == "suspended":
        return err("permission", "Account is suspended - escalate_to_human.")
    if o["refund_count"] > 0 or o["status"] == "refunded":
        return err("permission","Order already refunded. Explain this to the customer.")
    if args["amount_gbp"] > POLICY["auto_refund_limit_gbp"]:
        return err("permission",
                   f"GBP {args['amount_gbp']:.2f} exceeds the GBP {POLICY['auto_refund_limit_gbp']}"
                   "auto-refund authority- escalate_to_human.")

    pct = POLICY["condition_dispute_partial_pct"] / 100
    max_amount = round(o["price_gbp"] * pct, 2) if args['reason'] == "condition_partial" else o["price_gbp"]

    if args["amount_gbp"] <= 0:
        return err("validation", "Refund amount must be greater than zero." , retryable=True)
    if args["amount_gbp"]> max_amount:
        return err("validation", 
                   f"GBP {args['amount_gbp']:.2f} exceeds the maximum GBP {max_amount:.2f} "
                   f"for this order (item price GBP{o['price_gbp']:.2f}). "
                   "Re-check lookup_order and call again with the correct amount.",
                   retryable=True)


    # Policy guardrails, enforced here rather than trusted to the prompt.
    # These are declines, not escalations: the rule is unambiguous, so say no and explain.
    if o['category'] in POLICY["hygiene_excluded_categories"]:
        return err("permission", 
                   f"{o['category']} is hygiene-excluded and never returnable."
                   "Decline and explain - do not escalate.")

    delivered = date.fromisoformat(o["delivery_date"]) if o["delivery_date"] else None
    days = (TODAY - delivered).days if delivered else None
    if days is not None and days > POLICY["return_window_days"] and args['reason'] != "shop_fault":
        return err("permission", 
                   f"{days} days since delivery is outside the"
                   f"{POLICY['return_window_days']} day return window. Decline and explain - do not escalate.")

    if o["final_sale"] and args['reason'] != "shop_fault":
        return err("permission", 
                   "Final sale: refundable only when the shop is at fault. "
                   "Decline a change-of-mind return - do not escalate.")

    # condition disputes are self-serve only on cheap items.
    if args['reason'] == "condition_partial" and o["price_gbp"] > POLICY["condition_dispute_limit_gbp"]:
        return err("permission", 
                   f"Condition dispute on a GBP {o['price_gbp']:.2f} is above the GBP {POLICY['condition_dispute_limit_gbp']}"
                   "self-serve limit - escalate_to_human so a human can review photos.")

    status = "partially_refunded" if args["reason"] == "condition_partial" else "refunded"
    q("UPDATE orders SET status = ?, refund_count = refund_count + 1 WHERE order_id = ?",
          (status, args["order_id"]))
    return ok({"refunded": True, "order_id": args["order_id"],
               "amount_gbp": args["amount_gbp"], "new_status": status})



@tool(
    "escalate_to_human",
    "Hand the cases to human. Use when the case exceed automated authority: "
    f"refunds above GBP {POLICY['auto_refund_limit_gbp']}, authenticity claims(always), "
    f"delayed delivery, suspended account, condition disputes  "
    f"above GBP {POLICY['condition_dispute_limit_gbp']},"
    "Prefer escalating over guessing - a correct escalation beats an "
    "incorrect resolution. Terminal: do not call process_refund afterwards.",
    {
        "type": "object",
        "properties": {
            "order_id":  {"type": "string"},
            "reason_code": {"type": "string", "enum": [
                "above_refund_authority", "authenticity_claim", "condition_dispute",
                "delivery_dispute", "suspended_account", "other"]},
            "summary": {"type": "string", "description": "What the human needs to know."},
        },
        "required": ["reason_code", "summary"],
    },
)

async def escalate_to_human(args: dict[str, Any]) -> dict[str, Any]:
    ticket = f"ESC-{int(hashlib.md5(args['summary'].encode()).hexdigest(),16) % 10000:04d}"
    record = {
        "ticket_id":ticket,
        "created_at": TODAY.isoformat(),
        "order_id": args.get("order_id"),
        "reason_code": args["reason_code"],
        "summary": args["summary"],
    }
    with open(ESCALATIONS, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return ok({"escalated": True, "ticket_id": ticket})


# 6. system prompt
SHOP_SYSTEM_PROMPT = f"""You are the resolution agent for a second-hand fashion resale shop.
You handle returns,refunds and account questions end to end.

Shop policy (authoritative - these figures come from data/policy.json):
- Return window: {POLICY['return_window_days']} days from the delivery date, not the order date.
- Auto-refund limit: GBP {POLICY['auto_refund_limit_gbp']:.0f}. At or below this you may refund
 directly, above it, escalate.
- Condition disputes: when an item is materially worse than described, offer a
 "{POLICY['condition_dispute_partial_pct']}% partial refund, capped at GBP" 
 {POLICY['condition_dispute_limit_gbp']:.0f}. Beyond that cap, escalate.
- Hygiene-excluded categories, never returnable: {', '.join(POLICY['hygiene_excluded_categories'])}.
- Authenticity claims: always escalate. Never judge authenticity yourself.
- Shop fault overrides final sale: a final-sale item is still refundable when the shop is at fault
(wrong item sent, damaged in transit, misdescribed). It is not refundable for a change of mind.
- Stock is one-of-one, so exchanges are impossible. Say so plainly and offer a refund instead.

How to work:
1. Look before you decide. Call lookup_order for any order the customer names, and get_customer
   when account standing matters. Never guess dates, prices or refund history.
2. Resolve when the policy is clear - and that includes saying no. A return outside the window,
   a change-of-mind final sale, or a hygiene-excluded item is a clear decline, not an
   escalation. Explain which rule applies and why.
3. Escalate only when the policy genuinely runs out: authenticity claims, amounts above
   auto-refund limit,suspended accounts, delivery disputes, or a case the rules do not cover.
   Use escalate_to_human, and do not also refund the same concern.
4. Handle every concern. If a message raises several issues, address each one, then give
   a single reply covering them all rather than answering only the first.
5. Trust the thresholds. {POLICY['return_window_days']} days is inside the window and GBP 
   {POLICY['auto_refund_limit_gbp']:.0f} is within the limit - do not add caution the policy
   does not ask for.

Tone: warm, direct, plain English. State the outcome first, then the reason. When money
moves, say the amount and what happens next. Do not quote internal field names at the customer.
"""

SHOP_TOOL_DEFS = [get_customer, lookup_order, process_refund, escalate_to_human]

shop_server = create_sdk_mcp_server(name="shop", version="1.0.0", tools=SHOP_TOOL_DEFS)

# A literal brace means a forgotten f-prefix - the model would read the
# expression source instead of the number. Fail at import, not in an eval run.

for _t in SHOP_TOOL_DEFS:
    assert "{" not in _t.description, f"{_t.name}: unrendered brace in description"


# 7. Registry + lazy client

_PY_JSON = {str: "string", int: "integer", float: "number", bool: "boolean"}

def _to_json_schema(schema):
    if isinstance(schema,dict) and schema.get("type") == "object" and "properties" in schema:
        return schema

    props = {n: {"type": _PY_JSON[t]} for n, t in schema.items()}  # {"order_id": str}
    return {"type": "object", "properties": props, "required": list(props)}


SHOP_TOOLS = [
    {"name": t.name, "description": t.description,
     "input_schema": _to_json_schema(t.input_schema)}
     for t in SHOP_TOOL_DEFS
]

HANDLERS = {t.name: t.handler for t in SHOP_TOOL_DEFS}

_client = anthropic.Anthropic()

def client() -> anthropic.Anthropic:
    """The shared client for the raw-loop path."""
    return _client


# 8. dispatch and raw loop

async def dispatch(name: str, args: dict):
    handler = HANDLERS.get(name)
    if handler is None:
        return err("validation", f"Unknown tool {name!r}. Available: {', '.join(HANDLERS)}.")

    BUG_TYPES = (KeyError, NameError, TypeError, AttributeError, IndexError, ValueError)

    try:
        out = await handler(args)

    except Exception as e:
        retryable = not isinstance(e, BUG_TYPES)
        return err("transient", f"{name} raised {type(e).__name__}: {e}", retryable=retryable)
    
    if not isinstance(out, dict) or "content" not in out:
        return err("validation", f"{name} returned {out!r}, not a tool result.")

    return out


def text_of(resp):
    return "".join(b.text for b in resp.content if b.type == "text")

async def run_case_raw(message,max_turns=8):
    """The agentic loop. stop_reason decides whether to run tools or stop."""
    msgs = [{"role": "user", "content": message}]
    calls = []

    for _ in range(max_turns):
        resp = client().messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SHOP_SYSTEM_PROMPT,
            tools=SHOP_TOOLS,
            messages=msgs,
        )
        # Append the whole content list, never just the text: it carries the 
        # tool_use blocks - and the thinking blocks, which must replay unchanged.

        msgs.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            return calls, text_of(resp), "end_turn"

        if resp.stop_reason != "tool_use":
            # max_tokens or refusal - surface it instead of looping blindly
            return calls, text_of(resp), resp.stop_reason

        results = []

        for b in resp.content:
            if b.type != "tool_use":
                continue
            calls.append((b.name, b.input))
            out = await dispatch(b.name, b.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": b.id,
                "content": out["content"],
                "is_error": out.get("is_error", False),
            })

        # One user message holding every result - see the note above.

        msgs.append({"role": "user", "content": results})

    return calls, None, "max_turns"

#9 SDK loop


#interception PreToolUse hook
async def refund_authority_gate(input_data, tool_use_id, context):
    amount = input_data["tool_input"].get("amount_gbp", 0)
    limit = POLICY["auto_refund_limit_gbp"]
    if amount > limit:
        return {"hookSpecificOutput": {
            "hookEventName": input_data["hook_event_name"],
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"GBP {amount:.2f} exceeds the GBP {limit} auto-refund authority."
                "Call escalate_to_human with reason_code='above_refund_authority'."),
        }}
    return {} # empty dict = allow

options = ClaudeAgentOptions(
    mcp_servers={"shop": shop_server},
    allowed_tools=[f"mcp__shop__{t.name}" for t in SHOP_TOOL_DEFS],
    permission_mode="dontAsk",
    tools=[],
    system_prompt=SHOP_SYSTEM_PROMPT,
    hooks={"PreToolUse": [
        HookMatcher(matcher="mcp__shop__process_refund", hooks=[refund_authority_gate])
    ]},
)

async def run_case_sdk(message: str):
    calls, result = [], None
    async for msg in query(prompt=message, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    calls.append((block.name, block.input))
        elif isinstance(msg, ResultMessage):
            result = msg.result
    return calls, result

def run_sync(coro):
    """Run a coroutine from a notebook cell.

    Jupyter on Windows installs a SelectorEventLoop, which cannot spawn subprocesses - 
    and claude_agent_sdk runs the Claude Code CLI as one.
    So give the coroutine a fresh subprocess-capable loop on its own thread."""

    box = {}
    def target():
        loop = (asyncio.ProactorEventLoop() if sys.platform == "win32"
                else asyncio.new_event_loop())
        asyncio.set_event_loop(loop)
        try:
            box["out"] = loop.run_until_complete(coro)
        except BaseException as e:
            box["err"] = e
        finally:
            loop.close()

    t = threading.Thread(target=target)
    t.start(); t.join()
    if "err" in box:
        raise box["err"]
    return box["out"]


if __name__ == "__main__":
    with sandbox():
        calls, reply = run_sync(run_case_sdk(
            "Hi, I'd like to return the wrap dress from order ORD-001."
            "It doesn't suit me. Can I get a refund?"))

        for name, inp in calls:
            print(name, inp)
        print("\n---\n", reply)