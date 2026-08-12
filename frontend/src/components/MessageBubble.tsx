import type { ChatMessage } from "../types";

export function MessageBubble({ message }: { message: ChatMessage }) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-2xl rounded-lg px-4 py-3 text-sm ${
          isUser ? "bg-blue-600 text-white" : "bg-neutral-800 text-neutral-100"
        }`}
      >
        {/* Phase 0: plaintext only (SPEC.md §6 Phase 0 task 9). Markdown
            rendering + clickable citation chips arrive in Phase 1. */}
        <p className="whitespace-pre-wrap">{message.text}</p>

        {message.pending && message.statusLabel && (
          <p className="mt-2 animate-pulse text-xs text-neutral-400">{message.statusLabel}</p>
        )}

        {message.error && <p className="mt-2 text-xs text-red-400">{message.error}</p>}

        {!!message.sources?.length && (
          <div className="mt-3 flex flex-wrap gap-1 border-t border-neutral-700 pt-2">
            {message.sources.map((s, i) => (
              <span
                key={i}
                className="rounded bg-neutral-900 px-1.5 py-0.5 font-mono text-[11px] text-neutral-400"
                title={s.symbol ?? undefined}
              >
                {s.path}:{s.start_line}-{s.end_line}
              </span>
            ))}
          </div>
        )}

        {!!message.citations?.length && (
          <div className="mt-2 flex flex-wrap gap-1">
            {message.citations.map((c) => (
              <span
                key={c.id}
                className="rounded border border-neutral-600 px-1.5 py-0.5 font-mono text-[11px] text-neutral-300"
              >
                [{c.path}:{c.start_line}-{c.end_line}]
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
