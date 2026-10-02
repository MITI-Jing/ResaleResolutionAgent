"use client";

import { useEffect, useState } from "react";
import { sseStream } from "../lib/sse";

const API = process.env.NEXT_PUBLIC_API ?? "http://localhost:8000";

type Item =
  | { kind: "user" | "assistant" | "note"; text: string}
  | { kind: "tool"; name: string; args: unknown; ok?: boolean };

type Interrupt = {reason: string; tool_call: { name: string; args:Record<string, unknown> } };

export default function Home() {
  const [tid, setTid] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [pending, setPending] = useState<Interrupt | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState(
    "Hi, order ORD-001 arrived with a broken zip. I'd like a refund.");

  useEffect(() => {
  const saved = localStorage.getItem("thread_id") ?? crypto.randomUUID();
  localStorage.setItem("thread_id", saved);
  // localStorage is client-only; reading it must wait for mount(SSR hybration)
  // eslint-disable-next-line react-hooks/set-state-in-effect
  setTid(saved);
  // Rehydrate: a refresh mid-pause must come back to the approval card.
  fetch(`${API}/api/threads/${saved}`).then(r => r.json()).then(s => {
    if (s.status === "paused") setPending(s.interrupt);
    else if (s.status === "done" && s.reply) setItems([{ kind: "assistant", text: s.reply }]);
  }).catch(() => {});
  }, []);

  const push = (it: Item) => setItems(prev => [...prev, it]);
  const appendToken = (text: string) =>
  setItems(prev => {
    const last = prev[prev.length -1];
    if (last?.kind === "assistant")
      return [...prev.slice(0, -1), { ...last, text: last.text + text }];
    return [...prev, { kind: "assistant", text }];
  });

  async function run(body: object) {
  setBusy(true); setPending(null);
  try {
    for await (const ev of sseStream(`${API}/api/threads/${tid}/stream`, body)) {
      if (ev.event === "token") appendToken(ev.data.text);
      else if (ev.event === "tool_call")
        push({ kind: "tool", name: ev.data.name, args: ev.data.args });
      else if (ev.event == "tool_result")
        setItems(prev => prev.map(it =>
          it.kind === "tool" && it.name === ev.data.name && it.ok === undefined
            ? { ...it, ok: ev.data.ok } : it));
      else if (ev.event === "interrupt") setPending(ev.data);
      else if (ev.event === "error") push({ kind: "note", text: ev.data.message });
    }
  } finally { setBusy(false); }
  }

  const send = () => { push({ kind: "user", text: draft }); run({ message: draft }); };
  const decide = (approve: boolean) => {
  push({ kind: "note", text: approve ? "refund approved" : "refund denied" });
  run({ resume: { approve, note: "from UI"} });
  };
  const newThread = () => {
  const fresh = crypto.randomUUID();
  localStorage.setItem("thread_id", fresh);
  setTid(fresh); setItems([]); setPending(null);
  };

  return (
  <main style={{ maxWidth: 680, margin: "2rem auto", fontFamily: "system-ui", padding: "0 1rem"}}>
    <h1>Resale Resolution Agent</h1>
    <p style={{ color: "#777", fontSize: 13 }}>thread {tid.slice(0, 8)}
      <button onClick={newThread} style={{ marginLeft: 8}}>new thread</button></p>

    {items.map((it, i) => 
      it.kind === "tool" ? (
        <div key={i} style={{ fontFamily: "monospace", fontSize: 13, color: "#555", margin: "4px 0"}}>
          {it.ok === undefined ? "..." : it.ok ? "✓" : "✗"} {it.name}({JSON.stringify(it.args)})
        </div>
      ) : (
        <div key={i} style={{
          margin: "8px 0", padding: "8px 12px", borderRadius: 8, whiteSpace: "pre-wrap",
          background: it.kind === "user" ? "#e8f0fe" : it.kind === "note" ? "#fff8e1" : "#f4f4f4",
        }}>{it.text}</div>
      ))}

    {pending && (
      <div style={{ border: "2px solid #e8a000", borderRadius: 8, padding: 12, margin: "12px 0"}} >
        <strong>Approval needed</strong>
        <p>{pending.reason}</p>
        <code style={{ fontSize: 13 }}>{JSON.stringify(pending.tool_call.args)}</code>
        <div style={{ marginTop: 8 }}>
          <button onClick={() => decide(true)} disabled={busy}>Approve</button>
          <button onClick={() => decide(false)} disabled={busy} style={{ marginLeft: 8}}>Deny</button>
        </div>
      </div>
    )}

    <div style= {{ display: "flex", gap: 8, marginTop: 16 }}>
      <textarea value={draft} onChange={e => setDraft(e.target.value)}
      rows={2} style={{ flex: 1 }} disabled={busy} />
      <button onClick={send} disabled={busy || !!pending || !draft.trim()}>Send</button>
    </div>
  </main>
  );
}
