"""LangGraph loop for the Resale Support Resolution Agent.

Third loop beside run_case_raw(Messages API) and run_case_sdk(Claude Agent SDK).
Same 4 MCP tools, same dispatch, same policy.json, same system prompt, same 15 test cases.
Only the loop changes, so the eval rows are comparable.

    python eval.py --loop lg --out data/lg_r5.json --repeat 5
    python eval.py --loop lg-bedrock --out data/lg_bedrock_r5.json -- repeat 5  # Claude via Amazon Bedrock

Provider is a flag, not a rewrite: ChatAnthropic for the direct API, ChatBedrockConverse
for Bedrock. Tools, prompt and harness never see the difference.

Opt-in increments, not used by eval.py:
    - checkpointer: AsyncSqliteSaver, so a run survives the process and resumes by thread_id
    - gate: interrupt() before any process_refund above the auto-refund limit; a person decides.
    See demo_gate().
"""

from __future__ import annotations

import asyncio
import operator
import os
import time
import uuid
from typing import Annotated, Literal

import shop_agent as sa

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage, AIMessage
from langgraph.graph import StateGraph, MessagesState, START, END
from langgraph.types import Command, interrupt
from langchain_aws import ChatBedrockConverse
from langfuse.langchain import CallbackHandler
from langfuse import observe
from botocore.config import Config

Provider = Literal["anthropic", "bedrock"]

#1. Tools.

LC_TOOLS = [
    {"type": "function",
     "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
     for t in sa.SHOP_TOOLS
]

#2. Model behind a flag

def make_llm(provider: Provider = "anthropic"):
    if provider == "anthropic":
        llm = ChatAnthropic(model=sa.MODEL, max_tokens=16000, max_retries=5)
    elif provider == "bedrock":
        model_id = os.environ.get("BEDROCK_MODEL_ID")
        if not model_id:
            raise RuntimeError("set BEDROCK_MODEL_ID in .env to the Claude model id shown in the Bedrock console.")
        llm = ChatBedrockConverse(model=model_id, max_tokens=16000,
                                  region_name=os.environ.get("AWS_REGION", "eu-west-2"),
                                  config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
    else:
        raise ValueError(provider)
    return llm.bind_tools(LC_TOOLS)

def system_message(provider: Provider) -> SystemMessage:
    if provider == "anthropic":
        # Same cached prefix as run_case_raw: tools + system are the static part.
        return SystemMessage(content=[{"type": "text", "text": sa.SHOP_SYSTEM_PROMPT,
                                       "cache_control": {"type": "ephemeral"}}])
    return SystemMessage(content=sa.SHOP_SYSTEM_PROMPT)


#3. State: messages, plus the counters the raw loop keeps by hand.
#  operator.add makes each nodes' return an increment, not a replacement.

class LoopState(MessagesState):
    calls: Annotated[list, operator. add]
    api_calls: Annotated[int, operator.add]
    api_ms: Annotated[float, operator.add]
    tokens: Annotated[int, operator.add]


def text_of(msg: AIMessage) -> str:  # pulls out what model said
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")

def stop_reason_of(msg: AIMessage) -> str: # pulls out why model stopped
    meta = msg.response_metadata or {}
    return meta.get("stop_reason") or meta.get("stopReason") or "end_turn"  # Anthropic or Bedrock

def need_approval(tc: dict, threshold: float) -> bool:
    return (tc["name"] == "process_refund"
            and float(tc["args"].get("amount_gbp", 0)) > threshold)

def denial(tc: dict, decision: dict) -> ToolMessage:
    """Same envelope shape the model sees from err(), so its behaviour doesn't fork."""
    note = decision.get("note") or "no reason given"
    body = ('{"error": true, "errorCategory": "permission", "isRetryable": false, '
            f'"message": "Denied by approver: {note}. '
            'Call escalate_to_human with reason_code=above_refund_authority."}')
    return ToolMessage(content=body, tool_call_id=tc["id"], name=tc["name"], status="error")

#4. Graph
def build_graph(provider: Provider = "anthropic", max_turns: int = 8,
                gate: bool = False, checkpointer=None, gate_above: float | None = None):
    budget = sa.POLICY.get("run_token_budget", 60_000)
    threshold = sa.POLICY["auto_refund_limit_gbp"] if gate_above is None else gate_above
    llm = make_llm(provider)
    system = system_message(provider)

    async def agent(state: LoopState):
        t0 = time.perf_counter()
        ai = await llm.ainvoke([system] + state["messages"])
        u = ai.usage_metadata or {}
        return {"messages": [ai], "api_calls": 1, 
                "api_ms": (time.perf_counter() - t0) * 1000,
                "tokens": u.get("input_tokens", 0) + u.get("output_tokens", 0)}

    async def tools(state: LoopState):
        ai = state["messages"][-1]

        # Gate first, dispatch second. Everything before interrupt() re-runs when the run
        # resumes, so no side effect may sit in front of it.
        decisions = {}
        if gate:
            for tc in ai.tool_calls:
                if need_approval(tc, threshold):
                    decisions[tc["id"]] = interrupt({
                        "tool_call": tc,
                            "reason": f"GBP{tc['args']['amount_gbp']} exceeds approval threshold GBP{threshold}",
                    })

        out, calls = [], []
        if os.environ.get("SHOP_TOOLS_TRANSPORT") == "mcp":
            from mcp_dispatch import dispatch as DISPATCH
        else:
            DISPATCH = sa.dispatch
        for tc in ai.tool_calls:
            calls.append((tc["name"], tc["args"]))
            d = decisions.get(tc["id"])
            if d is not None and not d.get("approve"):
                out.append(denial(tc, d))
                continue
            env = await DISPATCH(tc["name"], tc["args"])
            out.append(ToolMessage(content=env["content"][0]["text"], tool_call_id=tc["id"],
                                    name=tc["name"], status="error" if env.get("is_error") else "success"))

        return {"messages": out, "calls": calls}

    def route_after_agent(state: LoopState):
        if state["api_calls"] >= max_turns or state["tokens"] >= budget:
            return END
        return "tools" if state["messages"][-1].tool_calls else END

    def route_after_tools(state: LoopState):
        return END if state["api_calls"] >= max_turns else "agent"

    g = StateGraph(LoopState)
    g.add_node("agent", agent)
    g.add_node("tools", tools)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent,{"tools": "tools", END: END})
    g.add_conditional_edges("tools", route_after_tools,{"agent": "agent", END: END})
    return g.compile(checkpointer=checkpointer)


_GRAPHS: dict = {}

def graph_for(provider: Provider, max_turns: int = 8):
    key = (provider, max_turns)
    if key not in _GRAPHS:
        _GRAPHS[key] = build_graph(provider, max_turns)
    return _GRAPHS[key]

def _initial(message: str) -> dict:
    return {"messages": [HumanMessage(content=message)], "calls": [], 
            "api_calls": 0, "api_ms": 0.0, "tokens": 0}


#5. The loop, same contract as run_case_raw: (calls, reply, stop_reason, usage)
@observe(name="langgraph")
async def run_case_lg(message: str, provider: Provider = "anthropic", max_turns: int = 8):
    t0 = time.perf_counter()
    final = await graph_for(provider, max_turns).ainvoke(
        _initial(message), config={"recursion_limit": 2 * max_turns + 2})

    msgs = final["messages"]
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for m in msgs:
        if not isinstance(m, AIMessage) or not m.usage_metadata:
            continue
        u = m.usage_metadata
        det = u.get("input_token_details") or {}
        cr, cc = det.get("cache_read") or 0, det.get("cache_creation") or 0
        # LangChain counts cached tokens inside input_tokens; the Anthropic API(and the raw
        # loop ) report them separately. Substract, so the cost columns compare like for like.
        usage["input_tokens"] += u.get("input_tokens", 0) - cr - cc
        usage["output_tokens"] += u.get("output_tokens", 0)
        usage["cache_read_input_tokens"] += cr
        usage["cache_creation_input_tokens"] += cc
    usage.update({"api_calls": final["api_calls"], "api_ms": round(final["api_ms"]),
                  "wall_ms": round((time.perf_counter() -t0) * 1000)})

    last = msgs[-1]
    if isinstance(last, ToolMessage):
        stop = "budget" if final["tokens"] >= sa.POLICY.get("run_token_budget", 60_000) else "max_turns"
        return final["calls"], None, "max_turns", usage
    return final["calls"], text_of(last), stop_reason_of(last), usage

# 6. Opt-in: checkpointed run with the approval gate. Pause -> decide -> resume, same thread.
async def demo_gate(message: str, thread_id: str | None = None, provider: Provider = "anthropic"):
    thread_id = thread_id or f"demo-{uuid.uuid4().hex[:8]}"
    print(f"thread: {thread_id}")
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    async with AsyncSqliteSaver.from_conn_string("checkpoints.db") as saver:
        graph = build_graph(provider, gate=True, checkpointer=saver, gate_above=0)
        cfg = {"configurable": {"thread_id": thread_id}, "recursion_limit": 18, "callbacks": [CallbackHandler()]}
        snap = await graph.aget_state(cfg)
        if snap.next: # paused mid-run in a previous process: re-ask, don't re-invoke
            state = {"__interrupt__": snap.tasks[0].interrupts}
        elif not message:
            print("no pending approval on this thread and no message given - nothing to do")
            return 
        else:
            state = await graph.ainvoke(_initial(message), cfg)
        while "__interrupt__" in state:
            ask = state["__interrupt__"][0].value
            print(f"\nPAUSED: {ask['reason']}  {ask['tool_call']['args']}")
            approve = input("approve? [y/N]").strip().lower() == "y"
            state = await graph.ainvoke(Command(resume={"approve": approve, "note": "cli"}), cfg)
        last = state["messages"][-1]
        if isinstance(last, ToolMessage):
            print("\n--- ended at max_turns; last tool result:\n", last.content)
        else:
            print("\n---\n", text_of(last))

if __name__ == "__main__":
    import sys
    tid = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        with sa.sandbox():
            asyncio.run(demo_gate("Hi, order ORD-001 arrived with a broken zip. I'd like a refund.",
                              thread_id=tid))
    except KeyboardInterrupt:
        print("\ninterrupted - resume with: python shop_agent_lg.py <thread-ids>")



