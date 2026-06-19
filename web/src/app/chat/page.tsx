"use client";
import { useState } from "react";
import { chat, chatStream, AuthError } from "@/lib/api";

export default function ChatPage() {
  const [history, setHistory] = useState<any[]>([]);
  const [input, setInput] = useState("");
  const [subject, setSubject] = useState("");
  const [busy, setBusy] = useState(false);

  function setLastAssistant(update: (m: any) => any) {
    setHistory((h) => {
      const copy = [...h];
      for (let i = copy.length - 1; i >= 0; i--) {
        if (copy[i].role === "assistant") { copy[i] = update(copy[i]); break; }
      }
      return copy;
    });
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    const base = [...history, { role: "user", content: text }];
    setHistory([...base, { role: "assistant", content: "", streaming: true }]);
    setInput("");
    setBusy(true);
    try {
      // Stream tokens into the last assistant bubble.
      const final = await chatStream(base, subject || undefined, (tok) =>
        setLastAssistant((m) => ({ ...m, content: m.content + tok })),
      );
      setLastAssistant((m) => ({
        ...m, streaming: false,
        content: m.content || "(no reply)",
        sources: final?.sources,
      }));
    } catch (err) {
      if (err instanceof AuthError) {
        setLastAssistant((m) => ({ ...m, streaming: false, content: "⚠ Please sign in to use the assistant." }));
      } else {
        // Fallback to the non-streaming endpoint on any stream error.
        try {
          const d = await chat(base, subject || undefined);
          setLastAssistant((m) => ({ ...m, streaming: false, content: d.reply || "(no reply)", sources: d.sources }));
        } catch (e2) {
          setLastAssistant((m) => ({ ...m, streaming: false, content: "⚠ " + String(e2) }));
        }
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h1>AI assistant</h1>
      <div className="card">
        <label>Focus subject (optional)</label>
        <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="SUBJ-0012" style={{ maxWidth: 220 }} />
      </div>

      <div className="card" style={{ minHeight: 240 }}>
        {history.length === 0 && <p className="muted">Ask about a subject&apos;s protocol visits, compliance, and trips. Replies stream live; identifiers are de-identified before reaching the model.</p>}
        {history.map((m, i) => (
          <div key={i} style={{ textAlign: m.role === "user" ? "right" : "left", margin: "8px 0" }}>
            <div style={{
              display: "inline-block", maxWidth: "85%", padding: "8px 12px", borderRadius: 10,
              whiteSpace: "pre-wrap",
              background: m.role === "user" ? "#2563eb" : "#e2e8f0",
              color: m.role === "user" ? "#fff" : "#1e293b",
            }}>{m.content}{m.streaming ? " ▍" : ""}</div>
            {m.sources?.length ? <div className="muted">sources: {m.sources.join(", ")}</div> : null}
          </div>
        ))}
      </div>

      <form onSubmit={send} style={{ display: "flex", gap: 8 }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask something…" />
        <button className="btn" disabled={busy}>{busy ? "…" : "Send"}</button>
      </form>
    </div>
  );
}
