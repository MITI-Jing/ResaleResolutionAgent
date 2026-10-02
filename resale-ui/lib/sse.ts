export type Interrupt = {
    reason: string;
    tool_call: { name: string; args: Record<string, unknown> };
};

export type SseEvent = 
    | { event: "token"; data: { text: string} }
    | { event: "tool_call"; data: { name: string; args: Record<string, unknown> } }
    | { event: "tool_result"; data: { name: string; ok: boolean } }
    | { event: "interrupt"; data: Interrupt}
    | { event: "error"; data: { message: string } };

export async function* sseStream(url: string, body: unknown):AsyncGenerator<SseEvent> {
    const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "", event = "message", data ="";
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";
        for (const raw of lines) {
            const line = raw.replace(/\r$/, "");
            if (line === ""){
                if (data) yield { event, data: JSON.parse(data) } as SseEvent;
                event = "message"; data = "";
            } else if (line.startsWith("event:")) event = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
            // lines starting with ":" are keep-alive comments - ignore
        }
    }
}