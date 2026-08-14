import { useEffect, useRef, useState } from "react";
import { streamQuery } from "../api/client";
import type { ChatMessage, Citation, ToolCallEvent } from "../types";
import { MessageBubble } from "./MessageBubble";

interface ChatPanelProps {
  repoId: string;
  onCitationClick: (citation: Citation) => void;
}

export function ChatPanel({ repoId, onCitationClick }: ChatPanelProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  // SPEC.md §6 Phase 3 task 5: a stable id for this conversation, so
  // follow-up questions ("how does it handle errors?") can be resolved
  // against the last few turns (retrieval/session.py). Regenerated
  // whenever the selected repo changes — a session is scoped to one
  // repo's conversation, not the whole app lifetime.
  const [sessionId, setSessionId] = useState(() => crypto.randomUUID());

  useEffect(() => {
    setMessages([]);
    setSessionId(crypto.randomUUID());
    inputRef.current?.focus();
  }, [repoId]);

  // SPEC.md §6 Phase 5 task 1 (keyboard nav): "/" jumps to the question
  // box from anywhere on the page — the same convention GitHub/Slack/etc.
  // use for "focus search". Skipped while focus is already inside a text
  // field (so it doesn't hijack a "/" someone is actually typing into the
  // question box or a repo-URL field) and while a request is in flight
  // (the box is disabled then anyway).
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "/" || busy) return;
      const active = document.activeElement;
      const isTyping = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement;
      if (isTyping) return;
      e.preventDefault();
      inputRef.current?.focus();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy]);

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

    const toolCalls: ToolCallEvent[] = [];

    await streamQuery(
      repoId,
      q,
      {
        onStatus: (stage, detail) => {
          updateMessage(assistantId, { statusLabel: detail || stage });
        },
        onSources: (chunks) => {
          updateMessage(assistantId, { sources: chunks });
        },
        onTool: (call) => {
          toolCalls.push(call);
          updateMessage(assistantId, { toolCalls: [...toolCalls] });
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
      },
      undefined,
      sessionId,
    );

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
          ref={inputRef}
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask a question about this repo… (press / to focus)"
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
