# 1. Header, imports, fixture reset
"""Eval harness: run all 15 cases and score by what actually changes.

Scoring reads the sandbox database and ticket file after each case rather
than parsing the reply. A process_refund call that a guard rejected is a
decline, not a refund - the trace alone cannot tell you that.

run: python eval.py                        # all 15 cases, raw Messages API loop
     python eval.py --loop sdk             # same cases through the Agent SDK
     python eval.py --only EC01 EC09
"""

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
import shop_agent as sa

sys.path.insert(0, str(Path(__file__).resolve().parent / "data"))
import build_dataset

ACTIONS =("resolve_refund", "resolve_partial", "resolve_decline", "escalate", "clarify")

# 2. snapshot and classify
def snapshot(tmp):
    """order_id -> status, read from the sandbox copy."""
    con = sqlite3.connect(tmp / "shop.db")
    try:
        return {row[0]: row[1] for row in con.execute("SELECT order_id, status FROM orders")}
    finally:
        con.close()


def tool_name(name):
    """mcp__shop__lookup_order -> lookup_order; raw loop names pass through."""
    return name.split("__")[-1]

def classify(calls, before, after, tmp, known_orders):
    """Map one run onto an expected_action label."""
    if (tmp / "escalations.jsonl").read_text(encoding="utf-8").strip():
        return "escalate"

    changed = [after[oid] for oid in after if before.get(oid) != after[oid]]
    if "partially_refunded" in changed:
        return "resolve_partial"
    if "refunded" in changed:
        return "resolve_refund"

    # Nothing moved. Did the agent chase an order that does not exist?
    referenced = {
        args.get("order_id")
        for name, args in calls
        if isinstance(args, dict) and args.get("order_id")
    }
    if referenced - known_orders:
        return "clarify"

    return "resolve_decline"

# 3. running one case
async def run_one(case, loop):
    with sa.sandbox() as tmp:
        known = set(snapshot(tmp))
        before = snapshot(tmp)

        if loop == "sdk":
            calls, reply = await sa.run_case_sdk(case["message"])
            stop = "sdk"
        else:
            calls, reply, stop, usage = await sa.run_case_raw(case["message"])

        after = snapshot(tmp)
        predicted = classify(calls, before, after, tmp, known)
        tickets = [json.loads(l) for l in
                   (tmp / "escalations.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]

    return {
        "case_id": case["case_id"],
        "expected": case["expected_action"],
        "predicted": predicted,
        "ok": predicted == case["expected_action"],
        "stop_reason": stop,
        "calls": [[tool_name(n), a] for n, a in calls],
        "tickets": tickets,
        "reply": reply,
        "usage": usage,
        "rationale": case["rationale"],   
    }

# 4. the metrics
def summarise(results):
    paid_out = {"resolve_refund", "resolve_partial"}

    resolvable = [r for r in results if r["expected"] != "escalate"]
    escalatable = [r for r in results if r["expected"] == "escalate"]

    return {
        "total": len(results),
        "exact_match": sum(r["ok"] for r in results),
        "fcr": (sum(r["ok"] for r in resolvable), len(resolvable)),
        "escalation_recall": (sum(r["predicted"] == "escalate" for r in escalatable), len(escalatable)),
        "false_escalations": [r["case_id"] for r in resolvable if r["predicted"] == "escalate"],
        "wrongly_paid_out": [r["case_id"] for r in results
                             if not r["ok"] and r["predicted"] in paid_out],
        "missed_escalations": [r["case_id"] for r in escalatable
                               if r["predicted"] != "escalate"],
        "tokens_in": sum(r["usage"].get("input_tokens", 0) for r in results),
        "tokens_out": sum(r["usage"].get("output_tokens", 0) for r in results), 
    }

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", choices=["raw", "sdk"], default="raw")
    ap.add_argument("--only", nargs="*", help="case_id prefixes, e.g. EC01 EC09")
    ap.add_argument("--out", default="data/eval_results.json")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    cases = build_dataset.build(quiet=True)    # resets the fixture AND returns the suite
    if args.only:
        cases = [c for c in cases if any(c["case_id"].startswith(p) for p in args.only)]

    results = []
    for run_idx in range(args.repeat):
        for case in cases:
            r = await run_one(case, args.loop)
            r["run"] = run_idx + 1
            results.append(r)
            mark = "PASS" if r["ok"] else "FAIL"
            label = r["case_id"] if args.repeat == 1 else f"{r['case_id']}#{run_idx + 1}"
            print(f"{mark}  {label:<32} expected={r['expected']:<16} got={r['predicted']}")

    s = summarise(results)
    print(f"\nexact match  {s['exact_match']}/{s['total']}")
    print(f"FCR            {s['fcr'][0]}/{s['fcr'][1]}  (cases the agent should close alone)")
    print(f"escalation recall {s['escalation_recall'][0]}/{s['escalation_recall'][1]}")

    if args.repeat > 1:
        print()
        for cid in dict.fromkeys(r["case_id"] for r in results):
            runs = [r for r in results if r["case_id"] == cid]
            spread = Counter(r["predicted"] for r in runs)
            detail = " ".join(f"{a}x{n}" for a, n in spread.most_common())
            print(f"{cid:<32} {sum(r['ok'] for r in runs)}/{len(runs)} pass {detail}")
            


    for key in ("false_escalations", "wrongly_paid_out", "missed_escalations"):
        if s[key]:
            print(f"{key:<18} {', '.join(s[key])}")

    Path(args.out).write_text(json.dumps(
        {"run_at": datetime.now(timezone.utc).isoformat(), "loop": args.loop,
         "summary": s, "results": results}, indent=2), encoding="utf-8", newline="\n")

    print(f"\nwrote {args.out}")
    return 0 if s["exact_match"] == s["total"] else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))