# Resale Support Resolution Agent

A customer support resolution agent for a second-hand fashion shop (clothes, shoes, bags), built with the Claude Agent SDK. The agent handles returns, refunds, and disputes against a synthetic backend, with target: **80%+ first-contact resolution while knowing when to escalate to a human.**

> **Status: in progress.** Dataset, test suite, four MCP tools with policy guardrails, two working agent loops (raw Messages API and Agent SDK), and a tool-layer smoke test. Next: the eval harness and results.

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

The escalation cases are not failures — a correct escalation beats an incorrect resolution. The eval harness (coming) will score FCR, escalation accuracy, and incorrect resolutions separately.

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

## Repo structure

```
shop_agent.py                    # tools, policy, system prompt, both agent loops — the importable module
test_tools.py                    # tool-layer smoke test: no API calls, no model
MCP_Tool_with_Escalation.ipynb   # narrative demo; imports shop_agent, defines nothing
data/
  build_dataset.py               # generates shop.db, test_cases.json, policy.json — also the reset button
  test_cases.json                # 15 cases: customer message + expected action + rationale
  policy.json                    # shop policy the prompt and tools both read (incl. precedence rules)
  escalations.jsonl              # escalation tickets written by escalate_to_human
  shop.db                        # SQLite backend — generated, gitignored; rebuild with build_dataset.py
requirements.txt
```

The notebook used to define the tool layer inline. It now imports it, so the notebook
and the eval harness cannot drift apart.

## Running it

```bash
pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
python data/build_dataset.py        # creates data/shop.db, test_cases.json, policy.json
python test_tools.py                # 37 checks, no API calls — run this first
jupyter lab MCP_Tool_with_Escalation.ipynb
```

`build_dataset.py` anchors its paths to its own directory, so it works from any cwd, and it
clears `escalations.jsonl` alongside the database — tickets are run state, not fixtures.

## The dataset is a fixture, so nothing may write to it

`process_refund` and `escalate_to_human` both write. Run the suite against the live database
and the fixture *is* the system under test: one case mutates the rows the next case is scored
against, and a second run scores differently from the first. `shop_agent.sandbox()` copies the
database and the ticket log to a temp directory and rebinds the module globals for the duration:

```python
with shop_agent.sandbox():
    calls, reply, stop = await shop_agent.run_case_raw(case["message"])
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
- [ ] Eval harness: run all 15 cases, score by tool calls made, not by parsing replies
- [ ] Results + failure analysis in this README

## Stack

Python · SQLite · Claude Agent SDK · Anthropic API
