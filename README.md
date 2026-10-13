# Resale Support Resolution Agent

A customer support resolution agent for a second-hand fashion shop (clothes, shoes, bags), built with the Claude Agent SDK. The agent handles returns, refunds, and disputes against a synthetic backend, with target: **80%+ first-contact resolution while knowing when to escalate to a human.**

> **Status: working end to end.** Dataset, test suite, 4 MCP tools with policy guardrails, three agent loops (raw Messages API, Agent SDK, LangGraph), a tool-layer smoke test, an eval harness that scores by database state, a human-approval web UI over a checkpointed graph, and a CI job that gates merges on the eval. Current: **75/75 exact match, 50/50 first-contact resolution** across 15 cases × 5 runs — in all three loops. See [Results](#results).

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

Four MCP tools, served in-process by default (`create_sdk_mcp_server`), so there is no subprocess or transport to manage — and over a standalone MCP server when a real transport is wanted (see [transport and backend](#the-transport-and-the-backend-are-flags-not-rewrites)):

| Tool | Role |
|---|---|
| `get_customer` | Account-level context: name, email, `account_status`, join date. Read-only. Deliberately returns no orders, so the model cannot confuse account facts with order facts. |
| `lookup_order` | The single source of truth for eligibility. Joins order + item and computes `days_since_delivery`, `in_return_window`, `already_refund` server-side — the model never does date arithmetic. Read-only. |
| `process_refund` | The only tool that moves money. Enforces every guardrail before writing: suspended account, duplicate refund, auto-refund limit, amount vs item price, hygiene exclusion, return window, final sale, condition-dispute limit. |
| `escalate_to_human` | Terminal. Writes a ticket to `data/escalations.jsonl` with a reason code and a summary for the human picking it up. |

Every tool returns a structured envelope rather than a bare string. Errors carry an `errorCategory` (`transient` / `validation` / `permission`) and an `isRetryable` flag, which is the difference between a model that usefully retries with a corrected amount and one that loops on a permission failure it can never satisfy.

## Three agent loops

The same agent is built three times, deliberately. The notebook walks the first two; the third lives in `shop_agent_lg.py`.

**1. Raw Messages API loop** — the mechanics, unabstracted. `stop_reason` drives control flow: `tool_use` runs the tools and feeds results back, `end_turn` returns, anything else (`max_tokens`, refusal) surfaces instead of looping blindly. The full assistant `content` list is appended each turn, never just the text, because it carries the `tool_use` and thinking blocks that must replay unchanged. A `dispatch` wrapper catches handler exceptions and classifies them — bug-shaped errors (`KeyError`, `TypeError`, …) come back non-retryable, everything else retryable — so a crash in a tool becomes a tool result the model can reason about instead of an exception that kills the run.

**2. Claude Agent SDK loop** — the same tools via `query()` and `ClaudeAgentOptions`, plus a `PreToolUse` hook (`refund_authority_gate`) that denies any `process_refund` above the authority limit before it executes. That check also exists inside the tool; the hook is defence in depth and shows where authority belongs when tools come from somewhere you don't control.

**3. LangGraph loop** — the same tools a third time, as a two-node `StateGraph` (`agent` ⇄ `tools`). The counters the raw loop keeps by hand — API calls, tokens, latency — live in graph state under `operator.add` reducers, so each node returns an increment instead of mutating anything. The provider is a flag, not a rewrite: `ChatAnthropic` for the direct API, `ChatBedrockConverse` for Claude on Amazon Bedrock (`--loop lg-bedrock`); tools, prompt and harness never see the difference. LangChain folds cached tokens into `input_tokens` where the Anthropic API reports them separately, so the loop subtracts them back out — otherwise its cost column would not compare like for like. Runs trace to Langfuse through a callback handler. Two opt-ins the eval never uses: a SQLite checkpointer, and an `interrupt()` approval gate — see [Human in the loop](#human-in-the-loop-pause-approve-resume).

All three loops share one system prompt, and every threshold in it is interpolated from `policy.json` — change the policy file and prompt, tools, and tests all move together.

> On Windows, Jupyter installs a `SelectorEventLoop`, which cannot spawn subprocesses — and the Agent SDK runs the Claude Code CLI as one. The `run_sync` helper runs the coroutine on a fresh `ProactorEventLoop` in its own thread.

## The transport and the backend are flags, not rewrites

The tools started in-process. Two environment variables move them out without touching agent code:

- **`SHOP_TOOLS_TRANSPORT=mcp`** routes every tool call through a standalone MCP server (`mcp_shop_server.py`, streamable HTTP on :8100) via `mcp_dispatch.py` — a drop-in replacement for `shop_agent.dispatch`, one stateless session per call. The extra hop shows up honestly in `wall_ms`.
- **`SHOP_BACKEND=sap`** points the server's reads at the live SAP S/4HANA Cloud sandbox (business partners and sales orders, over OData). The public sandbox rejects writes, so `process_refund` records to a local ledger (`data/sap_ledger.jsonl`) and the duplicate-refund check reads that ledger back. Same envelope, same guardrails in the tool layer — different system of record. `sap_probe.py` finds usable sandbox IDs; `mcp_smoke.py` round-trips client → :8100 → backend.

Evaluating over MCP has a trap this project walked into. The eval sandboxes each case by copying the database — but a separate server process keeps writing to the *real* one unless it is re-pointed at the copy. The server exposes a dev-only `use_fixture` tool for exactly that, but only when started with `SHOP_DEV=1`. One full run was recorded with the server started without it: the transcripts show every tool call succeeding (real tickets written, real rows updated), while the scorer — reading the untouched sandbox copy — reported 5/15, and the five "passes" were exactly the cases where "nothing changed" happens to be the right answer. `data/lg_mcp.json` is that run, kept as a harness artifact, not a result; the same loop in-process scores 75/75. The lesson: score-by-state assumes the state you read is the state the tools wrote, and a transport boundary can silently break that assumption.

## Human in the loop: pause, approve, resume

The `interrupt()` gate turns refund authority from "escalate and end" into "pause and ask". `build_graph(gate=True, checkpointer=...)` checkpoints every step to `checkpoints.db` (gitignored — it is run state); when a `process_refund` exceeds the threshold, the run suspends, and a `Command(resume={"approve": ...})` on the same thread id continues it — in the same process or any later one. A denial comes back to the model in the same error envelope the tools use (`errorCategory: permission`, pointing at `escalate_to_human`), so its behaviour doesn't fork on *who* said no. Everything before `interrupt()` re-runs on resume, so no side effect may sit in front of it — the gate runs before dispatch, not after.

Three ways in:

- **CLI** — `python shop_agent_lg.py`; Ctrl-C mid-pause, then `python shop_agent_lg.py <thread-id>` resumes from the checkpoint.
- **SSE API** — `uvicorn server:app --port 8000`: streams tokens, tool calls, tool results and the interrupt as separate events; `GET /api/threads/{tid}` tells a refreshed client where the thread stands (new / paused / done); a per-thread lock turns concurrent streams into a 409 instead of interleaved writes.
- **Web UI** — `resale-ui/`, a Next.js chat on :3000: streaming reply, tool-call rows, an approval card on pause, and rehydration so a refresh mid-pause comes back to the card, not a blank page.

The server runs against the real `data/shop.db`, not `sandbox()` — a paused thread must resume across restarts against the same database. Reset between demos with `python data/build_dataset.py`.

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

alongside 4 failure lists that name the exact run (`EC08#5`): `false_escalations` (caution
scored as safety), `wrongly_paid_out` (money that moved and shouldn't have), `missed_escalations`,
and `phantom_actions` (below).

`--repeat N` runs the suite N times and prints the per-case spread. That is not a nicety: at
n=1 this suite reported two failures that were variance and hid a defect that was real.

## Results

15 cases × 5 runs through each loop, same prompt (`prompt_sha 8eb40757f30e`),
same model — `data/raw_r5_v2.json` and `data/sdk_r5_v2.json`:

| Metric | Raw Messages API | Agent SDK | Target |
|---|---|---|---|
| Exact match | 75/75 | 75/75 | — |
| First-contact resolution | 50/50 | 50/50 | 80% |
| Escalation recall | 25/25 | 25/25 | — |
| Wrongly paid out | 0 | 0 | 0 |
| False escalations | 0 | 0 | 0 |
| Phantom actions | 0 | 0 | 0 |
| API calls / turns | 210 | 209 | — |
| Output tokens | 34,551 | 43,741 | — |
| Cache read / write | 456,247 / 2,183 | 573,931 / 41,984 | — |
| Cost | $1.45 | $1.69 | — |
| Latency p50 / p95 | 11.4s / 21.3s | 11.5s / 23.2s | — |

Both loops cache the static prefix. The raw loop's cost is computed from token
counts at Opus 5 list pricing; the SDK's is the CLI's own `total_cost_usd`,
which includes $0.05 of concurrent Haiku sidecar calls.

Latency is wall clock. The SDK also reports `duration_api_ms`, which is *not*
comparable: it exceeded wall clock on all 75 runs, because it sums overlapping
requests. The raw loop's calls are sequential, so its API time and wall clock
agree to within 13ms.

15 cases × 5 runs through the LangGraph loop, same prompt, same model (`data/lg_r5.json`):

| Metric | Result | Target |
|---|---|---|
| Exact match | 75/75 | — |
| First-contact resolution | 50/50 | 80% |
| Escalation recall | 25/25 | — |
| Wrongly paid out | 0 | 0 |
| False escalations | 0 | 0 |
| Phantom actions | 0 | 0 |
| Output tokens | 34,174 | — |
| Latency p50 / p95 | 10.9s / 21.6s | — |

Three orchestrators, one behaviour — which is the point: the decisions live in the
policy, the prompt and the tool layer, all shared, not in whichever framework runs
the loop.

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
shop_agent.py                    # tools, policy, system prompt, raw + SDK loops — the importable module
shop_agent_lg.py                 # LangGraph loop; Bedrock flag, checkpointer, approval gate
eval.py                          # eval harness: runs the suite, scores by database state
test_tools.py                    # tool-layer smoke test: no API calls, no model
mcp_shop_server.py               # standalone MCP server, streamable HTTP on :8100; SHOP_BACKEND=sap for SAP reads
mcp_dispatch.py                  # drop-in dispatch that calls the MCP server instead of in-process tools
mcp_smoke.py                     # round-trip check: client → :8100 → backend
sap_backend.py                   # S/4HANA Cloud sandbox reads, local ledger writes — same envelope, same guardrails
sap_probe.py                     # finds usable sandbox order/customer IDs
server.py                        # FastAPI SSE server for the gated loop: pause / approve / resume over HTTP
resale-ui/                       # Next.js chat UI: streaming, tool rows, approval card
MCP_Tool_with_Escalation.ipynb   # narrative demo; imports shop_agent, defines nothing
.github/workflows/ci.yml         # tool tests + UI build on every push and PR; eval gate on main
data/
  build_dataset.py               # generates shop.db, test_cases.json, policy.json — also the reset button
  test_cases.json                # 15 cases: customer message + expected action + rationale
  policy.json                    # shop policy the prompt and tools both read (incl. precedence rules)
  escalations.jsonl              # escalation tickets written by escalate_to_human
  shop.db                        # SQLite backend — generated, gitignored; rebuild with build_dataset.py
  sdk_r5.json, lg_r5.json, …     # eval results: summary + every run's calls, reply and usage
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
python eval.py --loop sdk --repeat 5 --out data/sdk_r5.json # Agent SDK loop
python eval.py --loop lg  --repeat 5 --out data/lg_r5.json  # LangGraph loop
python eval.py --loop lg-bedrock                            # same loop, Claude via Amazon Bedrock
python eval.py --only EC08 --repeat 15                      # one case, deep — for chasing variance
python eval.py --gate 13                                    # exit 0 at ≥13/15 — the CI threshold
```

Exit status is 0 only on a clean sweep, unless `--gate N` lowers the bar. A full 15 × 5 run is
75 model calls, roughly $2 at Opus pricing — cheap enough that there is no reason to test a
prompt change on a subset, and testing on a subset cannot see the regressions a shared prompt
causes elsewhere.

The human-in-the-loop demo (gate + checkpoints + UI):

```bash
uvicorn server:app --port 8000        # SSE API over the gated LangGraph loop
cd resale-ui && npm install && npm run dev   # chat UI on http://localhost:3000
```

The tools over a real MCP transport (two terminals; `SHOP_DEV=1` matters — see the
transport section for why):

```bash
SHOP_DEV=1 python mcp_shop_server.py                 # tools on :8100, plus the fixture re-point tool
SHOP_TOOLS_TRANSPORT=mcp python eval.py --loop lg    # eval through the server
```

`build_dataset.py` anchors its paths to its own directory, so it works from any cwd, and it
clears `escalations.jsonl` alongside the database — tickets are run state, not fixtures.

## CI

Three jobs in `.github/workflows/ci.yml`:

- **python-tests** — every push and PR: rebuild the seed database, run `test_tools.py`. Every
  guardrail branch, no API calls, free.
- **ui-build** — every push and PR: lint and production-build `resale-ui`.
- **eval-gate** — pushes to main only: `python eval.py --loop raw --gate 13`, full results JSON
  uploaded as an artifact either way. The gate sits at 13/15 rather than 15/15 deliberately:
  this suite has produced 1-in-5 variance failures that were not regressions, and a gate that
  cries wolf gets deleted. The job skips itself when `ANTHROPIC_API_KEY` is not configured, so
  forks without the secret stay green instead of failing on a missing key.

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
- [x] LangGraph loop — third implementation of the same contract; Bedrock behind a provider flag
- [x] Tools over a real MCP transport (streamable HTTP), with an SAP S/4HANA sandbox backend option
- [x] Human-in-the-loop approval: `interrupt()` + SQLite checkpoints, SSE server, Next.js UI
- [x] CI: tool tests + UI build per push, eval gate on main
- [ ] Re-run the eval over the MCP transport with the sandbox re-point in place


## Stack

Python · SQLite · Claude Agent SDK · Anthropic API · LangGraph · MCP · FastAPI · Next.js · Langfuse · GitHub Actions
