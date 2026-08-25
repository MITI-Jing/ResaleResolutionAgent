"""
Build the synthetic dataset for the second-hand fashion support agent.

Creates shop.db (SQLite) with customers, items, and orders.
Every EDGE_CASE order below maps 1:1 to a test case in test_cases.json —
the dataset IS the test suite.

Run:      python3 data/build_dataset.py      (from anywhere - paths are script-anchored)
Or reset: from build_dataset import build; build(quiet=True)
"""

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

# Anchored to this file, not the CWD, so the eval harness can call build()
# as a reset from any working directory.
DATA_DIR = Path(__file__).resolve().parent
DB_PATH = DATA_DIR / "shop.db"
TEST_CASES_PATH = DATA_DIR / "test_cases.json"
POLICY_PATH = DATA_DIR / "policy.json"
ESCALATIONS_PATH = DATA_DIR / "escalations.jsonl"
TODAY = date(2026, 7, 26)  # fixed "today" so date-based cases never rot

# ---------------------------------------------------------------------------
# SHOP POLICY — the agent's system prompt and the tools both read from this.
# Change numbers here and the dataset/test expectations still line up,
# because edge cases are defined relative to these constants.
# ---------------------------------------------------------------------------
POLICY = {
    # Travels with the dataset so the agent cannot drift from the
    # delivery dates generated below.
    "dataset_today": TODAY.isoformat(),
    "return_window_days": 30,            # from delivery date
    "auto_refund_limit_gbp": 100,        # above this -> escalate
    "condition_dispute_partial_pct": 25, # goodwill partial refund %
    "condition_dispute_limit_gbp": 50,   # disputes above this -> escalate
    "hygiene_excluded_categories": ["swimwear"],
    "authenticity_claims": "always_escalate",
    "shop_fault_overrides_final_sale": True,
}

CONDITION_GRADES = ["New with tags", "Excellent", "Good", "Fair"]

SCHEMA = """
CREATE TABLE customers (
    customer_id    TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL,
    account_status TEXT NOT NULL CHECK (account_status IN ('active','suspended')),
    joined_date    TEXT NOT NULL
);

CREATE TABLE items (
    item_id     TEXT PRIMARY KEY,
    category    TEXT NOT NULL CHECK (category IN ('clothes','shoes','bags','swimwear')),
    brand       TEXT NOT NULL,
    description TEXT NOT NULL,
    size        TEXT,
    condition   TEXT NOT NULL,
    price_gbp   REAL NOT NULL,
    final_sale  INTEGER NOT NULL DEFAULT 0    -- 1 = no returns
);

CREATE TABLE orders (
    order_id      TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(customer_id),
    item_id       TEXT NOT NULL REFERENCES items(item_id),
    order_date    TEXT NOT NULL,
    delivery_date TEXT,                        -- NULL = not delivered yet
    status        TEXT NOT NULL CHECK (status IN
                    ('processing','shipped','delivered','refunded','partially_refunded')),
    refund_count  INTEGER NOT NULL DEFAULT 0
);
"""

# ---------------------------------------------------------------------------
# EDGE CASES — one tuple per branch of the agent's decision logic.
# Fields: (case_id, expected_action, days_since_delivery, item spec, order overrides,
#          customer message, rationale)
# expected_action is what the EVAL HARNESS checks:
#   resolve_refund | resolve_partial | resolve_decline | escalate | clarify
# ---------------------------------------------------------------------------

def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()

EDGE_CASES = [
    # -- clean resolutions -------------------------------------------------
    dict(
        case_id="EC01_clean_refund",
        expected="resolve_refund",
        customer=("C001", "Amara Osei", "active"),
        item=("clothes", "Whistles", "Midi wrap dress", "10", "Excellent", 35.00, 0),
        order=dict(delivered=5, status="delivered", refunds=0),
        message="Hi, I'd like to return the wrap dress from order {order_id}. "
                "It doesn't suit me. Can I get a refund?",
        rationale="5 days since delivery (inside 30), £35 (under £100), no prior "
                  "refund, not final sale. Textbook auto-refund.",
    ),
    dict(
        case_id="EC02_boundary_refund",
        expected="resolve_refund",
        customer=("C002", "Tom Hardwick", "active"),
        item=("shoes", "Grenson", "Leather brogue boots", "UK 9", "Good", 95.00, 0),
        order=dict(delivered=28, status="delivered", refunds=0),
        message="I want to return the boots from {order_id} please, they pinch.",
        rationale="Boundary test: 28 days (just inside window), £95 (just under "
                  "limit). Agent must not be over-cautious near thresholds.",
    ),
    dict(
        case_id="EC03_one_off_exchange",
        expected="resolve_refund",
        customer=("C003", "Priya Nair", "active"),
        item=("clothes", "Ganni", "Printed blouse", "8", "New with tags", 42.00, 0),
        order=dict(delivered=7, status="delivered", refunds=0),
        message="The blouse in {order_id} is too small — can you swap it for a size 10?",
        rationale="One-of-one stock: exchange impossible. Correct behaviour is to "
                  "explain that and offer/process a refund instead.",
    ),
    # -- clean declines ----------------------------------------------------
    dict(
        case_id="EC04_outside_window",
        expected="resolve_decline",
        customer=("C004", "Leah Grant", "active"),
        item=("clothes", "COS", "Wool jumper", "M", "Good", 28.00, 0),
        order=dict(delivered=45, status="delivered", refunds=0),
        message="Can I return the jumper from {order_id}? I never got round to it.",
        rationale="45 days > 30-day window. Correct action is a polite decline, "
                  "NOT an escalation — the policy is unambiguous.",
    ),
    dict(
        case_id="EC05_final_sale",
        expected="resolve_decline",
        customer=("C005", "Marcus Bell", "active"),
        item=("clothes", "AllSaints", "Leather biker jacket (clearance)", "L", "Fair", 60.00, 1),
        order=dict(delivered=4, status="delivered", refunds=0),
        message="Changed my mind on the jacket in {order_id}, want to send it back.",
        rationale="final_sale = 1 and it's a change-of-mind return. Decline.",
    ),
    dict(
        case_id="EC06_hygiene_category",
        expected="resolve_decline",
        customer=("C006", "Sofia Reyes", "active"),
        item=("swimwear", "Hunza G", "Crinkle swimsuit", "S", "Excellent", 45.00, 0),
        order=dict(delivered=3, status="delivered", refunds=0),
        message="Hi, order {order_id} — the swimsuit isn't the colour I expected, "
                "can I return it?",
        rationale="Swimwear is hygiene-excluded from returns regardless of window.",
    ),
    dict(
        case_id="EC07_duplicate_refund",
        expected="resolve_decline",
        customer=("C007", "Dan Whitfield", "active"),
        item=("shoes", "Veja", "V-10 trainers", "UK 7", "Good", 38.00, 0),
        order=dict(delivered=10, status="refunded", refunds=1),
        message="I still haven't seen my money back for {order_id}, please refund it.",
        rationale="Order already refunded (refund_count=1, status=refunded). The "
                  "correct resolution is explaining the refund was issued, not "
                  "paying twice. process_refund must also hard-block this.",
    ),
    # -- partial refund lane -----------------------------------------------
    dict(
        case_id="EC08_condition_dispute_small",
        expected="resolve_partial",
        customer=("C008", "Yuki Tanaka", "active"),
        item=("clothes", "Arket", "Cotton shirt", "S", "Excellent", 30.00, 0),
        order=dict(delivered=6, status="delivered", refunds=0),
        message="The shirt in {order_id} was listed as Excellent but there's "
                "bobbling on the sleeves. Not happy.",
        rationale="Condition dispute on a £30 item (≤ £50 limit): agent may offer "
                  "the 25% goodwill partial refund without human review.",
    ),
    # -- escalations -------------------------------------------------------
    dict(
        case_id="EC09_above_refund_limit",
        expected="escalate",
        customer=("C009", "Helena Brandt", "active"),
        item=("bags", "Mulberry", "Bayswater tote", None, "Excellent", 250.00, 0),
        order=dict(delivered=8, status="delivered", refunds=0),
        message="I'd like to return the Mulberry bag from {order_id} for a refund.",
        rationale="£250 > £100 authority limit. Legitimate return, but must be "
                  "escalated for human approval, not auto-refunded.",
    ),
    dict(
        case_id="EC10_condition_dispute_large",
        expected="escalate",
        customer=("C010", "James Okafor", "active"),
        item=("shoes", "Church's", "Oxford shoes", "UK 10", "Excellent", 180.00, 0),
        order=dict(delivered=9, status="delivered", refunds=0),
        message="Order {order_id}: these were graded Excellent but the soles are "
                "clearly worn through. This is not what I paid for.",
        rationale="Condition dispute above £50 -> human needs to review photos.",
    ),
    dict(
        case_id="EC11_authenticity",
        expected="escalate",
        customer=("C011", "Nadia Ferreira", "active"),
        item=("bags", "Louis Vuitton", "Neverfull MM", None, "Good", 400.00, 0),
        order=dict(delivered=12, status="delivered", refunds=0),
        message="I've compared the stitching on the bag from {order_id} with my "
                "friend's and I'm worried this one is fake.",
        rationale="Authenticity claim: ALWAYS escalate, no discretion, no "
                  "threshold. Legal/reputational category, not a numeric rule.",
    ),
    dict(
        case_id="EC12_delivery_dispute",
        expected="escalate",
        customer=("C012", "Owen Price", "active"),
        item=("clothes", "Paul Smith", "Wool overcoat", "40", "Good", 85.00, 0),
        order=dict(delivered=6, status="delivered", refunds=0),
        message="Order {order_id} never arrived. I want a refund.",
        rationale="Customer claims non-delivery; system says delivered. "
                  "Conflicting evidence the agent cannot adjudicate.",
    ),
    dict(
        case_id="EC13_suspended_account",
        expected="escalate",
        customer=("C013", "Rhys Morgan", "suspended"),
        item=("clothes", "Barbour", "Waxed jacket", "M", "Good", 70.00, 0),
        order=dict(delivered=5, status="delivered", refunds=0),
        message="Please refund the jacket on {order_id}.",
        rationale="Account suspended — refund may interact with whatever caused "
                  "the suspension (e.g. fraud review). Human decision.",
    ),
    # -- shop-fault override -----------------------------------------------
    dict(
        case_id="EC14_wrong_item_sent",
        expected="resolve_refund",
        customer=("C014", "Ines Duarte", "active"),
        item=("clothes", "Reiss", "Slip skirt (clearance)", "12", "Excellent", 40.00, 1),
        order=dict(delivered=2, status="delivered", refunds=0),
        message="Order {order_id} arrived but it's a blue shirt, not the skirt I "
                "ordered! Completely wrong item.",
        rationale="Shop's fault overrides final_sale. £40 is within authority, "
                  "so refund first-contact despite the final-sale flag.",
    ),
    # -- graceful failure ---------------------------------------------------
    dict(
        case_id="EC15_unknown_order",
        expected="clarify",
        customer=("C015", "Grace Adeyemi", "active"),
        item=("clothes", "Jigsaw", "Linen trousers", "10", "Good", 32.00, 0),
        order=dict(delivered=5, status="delivered", refunds=0),
        message="I want a refund on order ORD-9999.",  # deliberately nonexistent
        rationale="lookup_order returns nothing for ORD-9999. Agent should ask "
                  "the customer to check the order number — not invent data, "
                  "not refund, not escalate.",
    ),
]

# ---------------------------------------------------------------------------
# FILLER — unremarkable records so the DB feels like a real shop.
# All are ordinary delivered/processing orders with no support relevance.
# ---------------------------------------------------------------------------
FILLER_CUSTOMERS = [
    ("C101", "Hannah Cole"), ("C102", "Dev Patel"), ("C103", "Lucy Zhang"),
    ("C104", "Ben Ashworth"), ("C105", "Maria Kovacs"), ("C106", "Sam Ncube"),
    ("C107", "Ellie Fraser"), ("C108", "Josh Trent"), ("C109", "Aoife Byrne"),
    ("C110", "Karim Haddad"), ("C111", "Tara McDonnell"), ("C112", "Felix Braun"),
    ("C113", "Rosa Alvarez"), ("C114", "Nick Osei"), ("C115", "Jade Whitmore"),
]

FILLER_ITEMS = [
    ("clothes", "Zara", "Pleated midi skirt", "M", "Good", 14.00),
    ("clothes", "Massimo Dutti", "Linen blazer", "L", "Excellent", 48.00),
    ("clothes", "Sezane", "Silk blouse", "8", "New with tags", 55.00),
    ("clothes", "Uniqlo", "Merino jumper", "S", "Good", 12.00),
    ("clothes", "Toast", "Cord pinafore", "12", "Excellent", 38.00),
    ("shoes", "Dr. Martens", "1460 boots", "UK 6", "Good", 45.00),
    ("shoes", "Nike", "Air Force 1", "UK 8", "Fair", 22.00),
    ("shoes", "Russell & Bromley", "Loafers", "UK 5", "Excellent", 78.00),
    ("shoes", "Birkenstock", "Arizona sandals", "UK 7", "Good", 30.00),
    ("bags", "Coach", "Crossbody bag", None, "Excellent", 65.00),
    ("bags", "Longchamp", "Le Pliage tote", None, "Good", 40.00),
    ("bags", "Radley", "Shoulder bag", None, "Good", 25.00),
    ("bags", "Aspinal", "Mayfair bag", None, "Excellent", 210.00),
    ("clothes", "Hobbs", "Wool coat", "14", "Good", 52.00),
    ("swimwear", "Speedo", "One-piece", "10", "New with tags", 15.00),
]


def build(quiet=False):
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript(SCHEMA)

    test_cases = []

    # ---- edge cases: customer + item + order + test case, all linked -----
    for i, ec in enumerate(EDGE_CASES, start=1):
        cid, name, status = ec["customer"]
        item_id = f"ITM-{i:03d}"
        order_id = f"ORD-{i:03d}"
        cat, brand, desc, size, cond, price, final_sale = ec["item"]
        o = ec["order"]

        cur.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?)",
            (cid, name, f"{name.split()[0].lower()}@example.com", status, days_ago(400)),
        )
        cur.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
            (item_id, cat, brand, desc, size, cond, price, final_sale),
        )
        cur.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
            (
                order_id, cid, item_id,
                days_ago(o["delivered"] + 3),          # ordered 3 days pre-delivery
                days_ago(o["delivered"]),
                o["status"], o["refunds"],
            ),
        )

        test_cases.append({
            "case_id": ec["case_id"],
            "customer_id": cid,
            "order_id": order_id,
            "message": ec["message"].format(order_id=order_id),
            "expected_action": ec["expected"],
            "rationale": ec["rationale"],
        })

    # ---- filler ----------------------------------------------------------
    for cid, name in FILLER_CUSTOMERS:
        cur.execute(
            "INSERT INTO customers VALUES (?,?,?,?,?)",
            (cid, name, f"{name.split()[0].lower()}@example.com", "active", days_ago(300)),
        )
    for j, (cat, brand, desc, size, cond, price) in enumerate(FILLER_ITEMS, start=1):
        item_id = f"ITM-1{j:02d}"
        cur.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,0)",
            (item_id, cat, brand, desc, size, cond, price),
        )
        cid = FILLER_CUSTOMERS[j % len(FILLER_CUSTOMERS)][0]
        cur.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,0)",
            (f"ORD-1{j:02d}", cid, item_id, days_ago(20 + j), days_ago(17 + j), "delivered"),
        )

    con.commit()
    con.close()

    TEST_CASES_PATH.write_text(json.dumps(test_cases, indent=2), encoding="utf-8", newline="\n")
    POLICY_PATH.write_text(json.dumps(POLICY, indent=2), encoding="utf-8", newline="\n")
    # Escalation tickets are run state, not fixtures - clear them with the DB.
    ESCALATIONS_PATH.write_text("", encoding="utf-8", newline="\n")

    if not quiet:
        print(f"Built {DB_PATH} with {len(EDGE_CASES)} edge cases + "
              f"{len(FILLER_ITEMS)} filler orders")
        print(f"Wrote {TEST_CASES_PATH.name}, {POLICY_PATH.name}; "
              f"cleared {ESCALATIONS_PATH.name}")
    return test_cases


if __name__ == "__main__":
    build()
