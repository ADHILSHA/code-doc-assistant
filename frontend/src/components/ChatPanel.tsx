import { useState } from "react";
import { streamQuery } from "../api/client";
import type { ChatMessage, Citation } from "../types";
import { MessageBubble } from "./MessageBubble";

interface ChatPanelProps {
  repoId: string;
  onCitationClick: (citation: Citation) => void;
}

export function ChatPanel({ repoId, onCitationClick }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);

  function updateMessage(id: string, patch: Partial<ChatMessage>) {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;

    setQuestion("");
    setBusy(true);

    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", text: q };
    const assistantId = crypto.randomUUID();
    const assistantMsg: ChatMessage = {
      id: assistantId,
      role: "assistant",
      text: "",
      pending: true,
      statusLabel: "thinking…",
    };
    setMessages((prev) => [...prev, userMsg, assistantMsg]);

    await streamQuery(repoId, q, {
      onStatus: (stage, detail) => {
        updateMessage(assistantId, { statusLabel: detail || stage });
      },
      onSources: (chunks) => {
        updateMessage(assistantId, { sources: chunks });
      },
      onToken: (text) => {
        setMessages((prev) =>
          prev.map((m) => (m.id === assistantId ? { ...m, text: m.text + text } : m)),
        );
      },
      onCitations: (citations) => {
        updateMessage(assistantId, { citations });
      },
      onDone: () => {
        updateMessage(assistantId, { pending: false, statusLabel: undefined });
      },
      onError: (message) => {
        updateMessage(assistantId, { pending: false, statusLabel: undefined, error: message });
      },
    });

    setBusy(false);
  }

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-1 flex-col gap-3 overflow-y-auto rounded-lg border border-neutral-800 bg-neutral-950 p-4">
        {messages.length === 0 && (
          <p className="text-sm text-neutral-500">
            Ask a question about this repo — e.g. "how do we hash passwords?"
          </p>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} onCitationClick={onCitationClick} />
        ))}
      </div>

      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about this repo…"
          disabled={busy}
          className="flex-1 rounded-md border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm
                     text-neutral-100 placeholder:text-neutral-500 focus:border-neutral-500
                     focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={busy || !question.trim()}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white
                     hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Ask
        </button>
      </form>
    </div>
  );
}
