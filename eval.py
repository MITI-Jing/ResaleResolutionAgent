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
import re
import math
import hashlib
import os

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

CLAIMS_PAID = re.compile(
    r"\b(i'?ve|i have|we'?ve|we have)\s+"
    r"(applied|processed|issued|refunded|put through|released|credited)\b", re.I)

NEGATED = re.compile(r"\b(no|not|haven'?t|hasn'?t|never|once|when|after|if|unless)\b"
                     r"[^.\n]{0,60}$", re.I)

def phantom_action(reply, calls):
    if not reply or any(tool_name(n) == "process_refund" for n, _ in calls):
        return False
    return any(not NEGATED.search(reply[:m.start()])
               for m in CLAIMS_PAID.finditer(reply))

def pct(values, p):
    """Nearest-rank percentile. Small n, so no interpolation."""
    if not values:
        return None
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]

# 3. running one case
async def run_one(case, loop):
    with sa.sandbox() as tmp:
        if os.environ.get("SHOP_TOOLS_TRANSPORT") == "mcp":
            from mcp_dispatch import dispatch as mcp_dispatch
            await mcp_dispatch("use_fixture", {"db_path": str(tmp / "shop.db"),
                                               "escalations_path": str(tmp / "escalations.jsonl")})
        known = set(snapshot(tmp))
        before = snapshot(tmp)

        if loop == "sdk":
            calls, reply, usage = await sa.run_case_sdk(case["message"])
            stop = "sdk"
        elif loop in ("lg", "lg-bedrock"):
            from shop_agent_lg import run_case_lg
            provider = "bedrock" if loop == "lg-bedrock" else "anthropic"
            calls, reply, stop, usage = await run_case_lg(case["message"], provider=provider)
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
        "phantom": phantom_action(reply, calls),   
    }

def label(r):
    """EC08#5 - case id plus run number, so a failure list points at one run."""
    return f"{r['case_id']}#{r['run']}"


# 4. the metrics
def summarise(results):
    paid_out = {"resolve_refund", "resolve_partial"}

    resolvable = [r for r in results if r["expected"] != "escalate"]
    escalatable = [r for r in results if r["expected"] == "escalate"]
    wall = [r["usage"]["wall_ms"] for r in results if r["usage"].get("wall_ms")]
    api = [r["usage"]["api_ms"] for r in results if r["usage"].get("api_ms")]

    return {
        "total": len(results),
        "exact_match": sum(r["ok"] for r in results),
        "fcr": (sum(r["ok"] for r in resolvable), len(resolvable)),
        "escalation_recall": (sum(r["predicted"] == "escalate" for r in escalatable), len(escalatable)),
        "false_escalations": [label(r) for r in resolvable if r["predicted"] == "escalate"],
        "wrongly_paid_out": [label(r) for r in results
                             if not r["ok"] and r["predicted"] in paid_out],
        "missed_escalations": [label(r) for r in escalatable
                               if r["predicted"] != "escalate"],

        "tokens_in": sum(r["usage"].get("input_tokens", 0) for r in results),
        "tokens_out": sum(r["usage"].get("output_tokens", 0) for r in results),
        "tokens_cache_read": sum(r["usage"].get("cache_read_input_tokens", 0) for r in results),
        "tokens_cache_write": sum(r["usage"].get("cache_creation_input_tokens", 0) for r in results), 
        "wall_ms": {"p50": pct(wall, 50), "p95": pct(wall, 95), "total": round(sum(wall))},
        "api_ms": {"p50": pct(api, 50), "p95": pct(api, 95), "total": round(sum(api))},
        "phantom_actions": [label(r) for r in results if r.get("phantom")],
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", choices=["raw", "sdk", "lg", "lg-bedrock"], default="raw")
    ap.add_argument("--only", nargs="*", help="case_id prefixes, e.g. EC01 EC09")
    ap.add_argument("--out", default="data/eval_results.json")
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()

    cases = build_dataset.build(quiet=True)    # resets the fixture AND returns the suite
    if args.only:
        cases = [c for c in cases if any(c["case_id"].startswith(p) for p in args.only)]

    started = datetime.now(timezone.utc).isoformat()
    prompt_sha = hashlib.sha256(
        (sa.SHOP_SYSTEM_PROMPT + json.dumps(sa.POLICY, sort_keys=True)).encode()
    ).hexdigest()[:12]
    out = Path(args.out)

    def dump():
        """Written after every case - a crash one case, not the suite."""
        out.write_text(json.dumps(
            {"run_at":started, "loop": args.loop, 
             "model": os.environ.get("BEDROCK_MODEL_ID") if args.loop == "lg-bedrock" else sa.MODEL,
             "prompt_sha": prompt_sha, "summary": summarise(results),
             "results": results}, indent=2), encoding="utf-8", newline="\n")

    results = []

    for run_idx in range(args.repeat):
        for case in cases:
            try:
                r = await run_one(case, args.loop)
            except Exception as e:
                r= {"case_id": case["case_id"], "expected": case["expected_action"],
                    "predicted": "error", "ok": False, "stop_reason": "error",
                    "error": f"{type(e).__name__}: {e}", "calls":[], "tickets": [],
                    "reply": None, "usage": {}, "rationale": case["rationale"],
                    "phantom":False}
            r["run"] = run_idx + 1
            results.append(r)
            dump()

            mark = "PASS" if r["ok"] else "FAIL"
            name = r["case_id"] if args.repeat == 1 else label(r)
            print(f"{mark}  {name:<32} expected={r['expected']:<16} got={r['predicted']}")

    s = summarise(results)
    print(f"\nexact match  {s['exact_match']}/{s['total']}")
    print(f"FCR            {s['fcr'][0]}/{s['fcr'][1]}  (cases the agent should close alone)")
    print(f"escalation recall {s['escalation_recall'][0]}/{s['escalation_recall'][1]}")
     

    if s["api_ms"]["p50"] is not None:
        print(f"latency   api p50 {s['api_ms']['p50']/1000:.1f}s"
              f" p95  {s['api_ms']['p95']/1000:.1f}s"
              f"  wall p50 {s['wall_ms']['p50']/1000:.1f}s"
              f"  p95 {s['wall_ms']['p95']/1000:.1f}s")


    if args.repeat > 1:
        print()
        for cid in dict.fromkeys(r["case_id"] for r in results):
            runs = [r for r in results if r["case_id"] == cid]
            spread = Counter(r["predicted"] for r in runs)
            detail = " ".join(f"{a}x{n}" for a, n in spread.most_common())
            print(f"{cid:<32} {sum(r['ok'] for r in runs)}/{len(runs)} pass {detail}")
            


    for key in ("false_escalations", "wrongly_paid_out", "missed_escalations","phantom_actions"):
        if s[key]:
            print(f"{key:<18} {', '.join(s[key])}")


    print(f"\nwrote {args.out}")
    return 0 if s["exact_match"] == s["total"] else 1

if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
from langfuse import get_client
get_client().flush()
