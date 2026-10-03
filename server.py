"""SSE server for the gated LangGraph loop.
    uvicorn server: app --port 8000

Runs against the real data/shop.db - NOT sandbox(), because a paused thread must
resume across retarts again the same database. Reset between demos with 
python data/build_dataset.py
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from collections import defaultdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from langfuse.langchain import CallbackHandler
from langchain_core.messages import AIMessage, AIMessageChunk

from shop_agent_lg import build_graph, _initial, text_of

LOCKS: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # One saver for the app's life - a per-request context manager would close
    # the DB a paused thread needs later.
    async with AsyncSqliteSaver.from_conn_string("checkpoints.db") as saver:
        app.state.graph = build_graph(gate=True, checkpointer=saver, gate_above=0)
        yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware,
                   allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
                   allow_methods=["*"], allow_headers=["*"])

class Approval(BaseModel):
    approve: bool
    note: str = ""

class RunInput(BaseModel):
    message: str | None = None
    resume: Approval | None = None

def cfg_for(tid: str) -> dict:
    return {"configurable": {"thread_id": tid}, "recursion_limit": 18,
            "callbacks": [CallbackHandler()]}

def chunk_text(msg) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c
                   if isinstance(b, dict) and b.get("type") == "text")

def sse(event: str, data) -> dict:
    return {"event": event, "data": json.dumps(data)}

@app.get("/api/threads/{tid}")
async def thread_state(tid: str):
    """Where the thread stands - lets the UI rehydrate after a refresh."""
    snap = await app.state.graph.aget_state(cfg_for(tid))
    if not snap.values:
        return {"status": "new"}
    if snap.next:
        ints = snap.tasks[0].interrupts
        return {"status": "paused", "interrupt": ints[0].value if ints else None}
    return {"status": "done", "reply": text_of(snap.values["messages"][-1])}

@app.get("/")
async def root():
    return {"service": "resale-resolution-agent", "docs": "/docs"}

@app.post("/api/threads/{tid}/stream")
async def stream(tid: str, body: RunInput):
    graph = app.state.graph
    if body.resume is not None:
        inp = Command(resume=body.resume.model_dump())
    elif body.message:
        inp = _initial(body.message)
    else:
        raise HTTPException(422, "send either message or resume")
    lock = LOCKS[tid]
    if lock.locked():
        raise HTTPException(409, "thread is already streaming - wait for it to finish")

    async def gen():
        async with lock:
            cfg = cfg_for(tid)
            try:
                async for mode, chunk in graph.astream(inp, cfg,
                                                    stream_mode=["messages", "updates"]):
                    if mode == "messages":
                        msg, _meta = chunk
                        if isinstance(msg, (AIMessage, AIMessageChunk)) and (text := chunk_text(msg)):
                            yield sse("token", {"text": text})
                        continue
                    if "__interrupt__" in chunk:
                        yield sse("interrupt", chunk["__interrupt__"][0].value)
                        continue
                    if "agent" in chunk:
                        for tc in chunk["agent"]["messages"][-1].tool_calls:
                            yield sse("tool_call", {"name": tc["name"], "args": tc["args"]})
                    if "tools" in chunk:
                        for tm in chunk["tools"]["messages"]:
                            yield sse("tool_result", {"name": tm.name,
                                                    "ok": tm.status != "error"})
                snap = await graph.aget_state(cfg)
                if snap.next:
                    yield sse("done", {"status": "paused"})
                else:
                    yield sse("done", {"status": "complete",
                                "reply": text_of(snap.values["messages"][-1])})
            except Exception as e:
                yield sse("error", {"message": f"{type(e).__name__}: {e}"})

    return EventSourceResponse(gen())
