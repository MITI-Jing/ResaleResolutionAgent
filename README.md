# Resale Support Resolution Agent

A customer support resolution agent for a second-hand fashion shop (clothes, shoes, bags), built with the Claude Agent SDK. The agent handles returns, refunds, and disputes against a synthetic backend, with target: **80%+ first-contact resolution while knowing when to escalate to a human.**

> **Status: working end to end.** Dataset, test suite, four MCP tools with policy guardrails, two agent loops (raw Messages API and Agent SDK), a tool-layer smoke test, and an eval harness that scores by database state. Current: **75/75 exact match, 50/50 first-contact resolution** across 15 cases × 5 runs. See [Results](#results).

## Design principle 1: the dataset is the test suite

Every record in the database exists for a reason. Fifteen hand-designed edge cases each target one branch of the agent's decision logic, and each maps 1:1 to a test case with an expected outcome:

| Expected action | Cases | Examples |
|---|---|---|
| `resolve_refund` | 4 | clean return; boundary test (28 days, £95); exchange request on one-of-one stock; wrong item sent overriding final-sale |
| `resolve_decline` | 4 | outside 30-day window; final-sale; hygiene-excluded swimwear; duplicate refund attempt |
| `resolve_partial` | 1 | condition dispute on a £30 item (goodwill partial refund) |
| `escalate` | 5 | refund above £100 authority; condition dispute above £50; authenticity claim (always); delivery dispute; suspended account |
| `clarify` | 1 | nonexistent order number — agent must ask, not hallucinate |

Edge cases and test cases are generated from a single source in `data/build_dataset.py`, so they can never drift out of sync. Thresholds live in `data/policy.json`; cases are defined relative to them, so the suite survives policy changes.

The escalation cases are not failures — a correct escalation beats an incorrect resolution. The eval harness scores FCR, escalation accuracy, and incorrect resolutions separately, so a cautious agent that escalates everything cannot hide behind a single aggregate.

## Design principle 2: a decline is a resolution, not an escalation

The easy failure mode is an agent that escalates everything it feels uneasy about, which scores zero on first-contact resolution while looking cautious. So the tool layer distinguishes two kinds of "no":

- **Decline** — the rule is unambiguous. Outside the return window, change-of-mind on a final-sale item, hygiene-excluded category, already refunded. The agent says no, names the rule, and closes the case.
- **Escalate** — the policy genuinely runs out. Above the £100 auto-refund authority, authenticity claims (always), condition disputes above £50, delivery disputes, suspended accounts.

Both are enforced in `process_refund` rather than trusted to the prompt, and the error messages tell the model which branch it landed in (`"Decline and explain — do not escalate"` vs `"escalate_to_human"`). Policy that only lives in a system prompt is a suggestion; policy in the tool layer is a guarantee.

## The tools

Four MCP tools, served over an in-process SDK MCP server (`create_sdk_mcp_server`), so there is no subprocess or transport to manage:

| Tool | Role |
|---|---|
| `get_customer` | Account-level context: name, email, `account_status`, join date. Read-only. Deliberately returns no orders, so the model cannot confuse account facts with order facts. |
| `lookup_order` | The single source of truth for eligibility. Joins order + item and computes `days_since_delivery`, `in_return_window`, `already_refund` server-side — the model never does date arithmetic. Read-only. |
| `process_refund` | The only tool that moves money. Enforces every guardrail before writing: suspended account, duplicate refund, auto-refund limit, amount vs item price, hygiene exclusion, return window, final sale, condition-dispute limit. |
| `escalate_to_human` | Terminal. Writes a ticket to `data/escalations.jsonl` with a reason code and a summary for the human picking it up. |

Every tool returns a structured envelope rather than a bare string. Errors carry an `errorCategory` (`transient` / `validation` / `permission`) and an `isRetryable` flag, which is the difference between a model that usefully retries with a corrected amount and one that loops on a permission failure it can never satisfy.

## Two agent loops

The notebook builds the same agent twice, deliberately.

**1. Raw Messages API loop** — the mechanics, unabstracted. `stop_reason` drives control flow: `tool_use` runs the tools and feeds results back, `end_turn` returns, anything else (`max_tokens`, refusal) surfaces instead of looping blindly. The full assistant `content` list is appended each turn, never just the text, because it carries the `tool_use` and thinking blocks that must replay unchanged. A `dispatch` wrapper catches handler exceptions and classifies them — bug-shaped errors (`KeyError`, `TypeError`, …) come back non-retryable, everything else retryable — so a crash in a tool becomes a tool result the model can reason about instead of an exception that kills the run.

**2. Claude Agent SDK loop** — the same tools via `query()` and `ClaudeAgentOptions`, plus a `PreToolUse` hook (`refund_authority_gate`) that denies any `process_refund` above the authority limit before it executes. That check also exists inside the tool; the hook is defence in depth and shows where authority belongs when tools come from somewhere you don't control.

Both loops share one system prompt, and every threshold in it is interpolated from `policy.json` — change the policy file and prompt, tools, and tests all move together.

> On Windows, Jupyter installs a `SelectorEventLoop`, which cannot spawn subprocesses — and the Agent SDK runs the Claude Code CLI as one. The `run_sync` helper runs the coroutine on a fresh `ProactorEventLoop` in its own thread.

## Design principle 3: score what changed, not what was said

`eval.py` runs each case in a fresh sandbox and classifies the outcome by reading the database and
the ticket log afterwards — never by parsing the reply. A `process_refund` call that a guard
rejected is a decline, not a refund, and the trace alone cannot tell you which. A ticket in
`escalations.jsonl` is `escalate`; a changed order status is `resolve_refund` or
`resolve_partial`; a reference to an order that does not exist is `clarify`; anything else is
`resolve_decline`.

That last branch is a fallback, not a detection — "the agent said no" and "nothing happened" land
on the same label. Scoring is unaffected, since a case expecting a refund fails either way, but
reading the label alone will mislead you, and twice it did.

Three numbers are reported separately, because one aggregate hides the trade-off the agent is
actually making:

- **exact match** — predicted label equals expected label
- **FCR** — of the cases the agent should close alone, how many it closed correctly
- **escalation recall** — of the cases it should hand off, how many it handed off

alongside four failure lists that name the exact run (`EC08#5`): `false_escalations` (caution
scored as safety), `wrongly_paid_out` (money that moved and shouldn't have), `missed_escalations`,
and `phantom_actions` (below).

`--repeat N` runs the suite N times and prints the per-case spread. That is not a nicety: at
n=1 this suite reported two failures that were variance and hid a defect that was real.

## Results

15 cases × 5 runs through the Agent SDK loop (`data/sdk_r5.json`):

| Metric | Result | Target |
|---|---|---|
| Exact match | 75/75 | — |
| First-contact resolution | 50/50 | 80% |
| Escalation recall | 25/25 | — |
| Wrongly paid out | 0 | 0 |
| False escalations | 0 | 0 |
| Phantom actions | 0 | 0 |

n=5 per case bounds a case's true pass rate above roughly 0.55. That rules out defects the size of
the ones this suite actually found — it does not rule out a 1-in-20 defect.

### Repeated runs found defects that a single pass reported as passes

| Run | Prompt | Result | What it showed |
|---|---|---|---|
| SDK, n=1 | initial | 13/15 | EC03 and EC07 look like hard failures |
| SDK, n=5 (EC03, EC07) | initial | 7/10 | both are variance: EC03 4/5, EC07 3/5 |
| SDK, n=5 | + decline rules | 74/75 | EC08 phantom action, 1/5 |
| Raw, n=5 | + decline rules | 74/75 | same defect, same case, same rate |
| SDK, n=15 (EC08) | + phantom rule | 15/15 | no recurrence |
| SDK, n=5 | + phantom rule | 75/75 | clean sweep |

Neither of the first run's two failures was what it looked like. **EC03** was labelled
`resolve_decline`, but it had not declined: it explained that one-of-one stock makes an exchange
impossible, offered the £42 refund, and ended with *"Want me to go ahead?"* — then stopped, because
there is no second turn. **EC07** had not paid twice; it correctly refused the duplicate refund and
*also* opened a payment-trace ticket, which the harness counts as a false escalation. Both were
fixed in the system prompt — the agent gets one turn and no follow-up message, and "already
refunded" joined the window, final-sale and hygiene rules in the list of unambiguous declines.

### The failure only a state-based scorer could see

On one run of EC08, the agent wrote:

> I've applied a **25% partial refund of £7.50** on ORD-008 … That'll go back to your original
> payment method and typically shows up in 3–5 working days.

Its `calls` list holds one entry: `lookup_order`. It never called `process_refund`. No money moved,
and the customer was told it had.

That is worse than the EC03 failure it superficially resembles. EC03 asked permission and correctly
left the money alone; this one claims a completed action that never happened. Any eval that scores
the reply text — or reads the trace loosely — marks the run a pass.

It reproduced in both loops at the same rate, 1/5 each, which rules out the orchestration layer and
the SDK's larger reasoning budget as the cause. A prompt rule (*never tell the customer a refund is
done unless `process_refund` returned success*) has held for 20 runs since, but 2/10 → 0/15 is
p ≈ 0.15 by Fisher's exact test — suggestive, not proof. The durable fix is the detector rather than
the rule: `phantom_actions` flags any reply claiming a completed refund with no matching tool call,
so a recurrence surfaces on its own instead of scoring as a pass.

The detector is a screen, not a scorer. Its first version flagged 12 runs, 11 of them false
positives — it matched "refund has been issued" inside "**no** refund has been issued", which is the
escalation cases correctly reporting refund history in their handoff summaries. Matching prose for
claims about state is fragile in exactly that way, so the flag surfaces candidates to read rather
than feeding a number.

## Repo structure

```
shop_agent.py                    # tools, policy, system prompt, both agent loops — the importable module
eval.py                          # eval harness: runs the suite, scores by database state
test_tools.py                    # tool-layer smoke test: no API calls, no model
MCP_Tool_with_Escalation.ipynb   # narrative demo; imports shop_agent, defines nothing
data/
  build_dataset.py               # generates shop.db, test_cases.json, policy.json — also the reset button
  test_cases.json                # 15 cases: customer message + expected action + rationale
  policy.json                    # shop policy the prompt and tools both read (incl. precedence rules)
  escalations.jsonl              # escalation tickets written by escalate_to_human
  shop.db                        # SQLite backend — generated, gitignored; rebuild with build_dataset.py
  sdk_r5.json, raw_r5.json, …    # eval results: summary + every run's calls, reply and usage
requirements.txt
```

The notebook used to define the tool layer inline. It now imports it, so the notebook
and the eval harness cannot drift apart.

## Running it

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
python data/build_dataset.py        # creates data/shop.db, test_cases.json, policy.json
python test_tools.py                # tool-layer checks, no API calls — run this first
jupyter lab MCP_Tool_with_Escalation.ipynb
```

The eval harness:

```bash
python eval.py                                              # 15 cases, raw Messages API loop
python eval.py --loop sdk --repeat 5 --out data/sdk_r5.json # the run reported above
python eval.py --only EC08 --repeat 15                      # one case, deep — for chasing variance
```

Exit status is 0 only on a clean sweep. A full 15 × 5 run is 75 model calls, roughly $2 at Opus
pricing — cheap enough that there is no reason to test a prompt change on a subset, and testing on
a subset cannot see the regressions a shared prompt causes elsewhere.

`build_dataset.py` anchors its paths to its own directory, so it works from any cwd, and it
clears `escalations.jsonl` alongside the database — tickets are run state, not fixtures.

## The dataset is a fixture, so nothing may write to it

`process_refund` and `escalate_to_human` both write. Run the suite against the live database
and the fixture *is* the system under test: one case mutates the rows the next case is scored
against, and a second run scores differently from the first. `shop_agent.sandbox()` copies the
database and the ticket log to a temp directory and rebinds the module globals for the duration:

```python
with shop_agent.sandbox():
    calls, reply, stop, usage = await shop_agent.run_case_raw(case["message"])
```

Read `shop_agent.DB` at call time — `from shop_agent import DB` binds a copy and misses the rebind.

`TODAY` is pinned to 2026-07-26 so the return-window boundary cases stay meaningful. It ships
*inside* `policy.json`, written there by `build_dataset.py` and read back by `shop_agent`, so it
cannot drift from the delivery dates the dataset was generated with.

## Roadmap

- [x] Synthetic dataset + policy + test suite
- [x] Backend tools: `get_customer`, `lookup_order`, `process_refund`, `escalate_to_human` — with policy guardrails enforced in the tool layer, not just the prompt
- [x] Agent loop (stop_reason-controlled, tool results fed back into context)
- [x] Agent SDK variant with a `PreToolUse` authority hook
- [x] Tool layer extracted to `shop_agent.py`; dataset isolated per run via `sandbox()`
- [x] Tool-layer smoke test covering every guardrail branch
- [x] Eval harness: run all 15 cases, score by database state, not by parsing replies
- [x] Repeated runs (`--repeat`) with per-case variance, and a phantom-action detector
- [x] Results + failure analysis in this README


## Stack

Python · SQLite · Claude Agent SDK · Anthropic API
