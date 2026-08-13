import type { ChatMessage, Citation } from "../types";
import { CitationChip } from "./CitationChip";
import { ToolTrail } from "./ToolTrail";

interface MessageBubbleProps {
  message: ChatMessage;
  onCitationClick: (citation: Citation) => void;
}

export function MessageBubble({ message, onCitationClick }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-2xl rounded-lg px-4 py-3 text-sm ${
          isUser ? "bg-blue-600 text-white" : "bg-neutral-800 text-neutral-100"
        }`}
      >
        {/* Plaintext answer body (SPEC.md §6 Phase 0 task 9 scoped this to
            plaintext; Phase 1 task 5 only asks for citations to become
            clickable chips, not full markdown rendering — see DECISIONS.md). */}
        <p className="whitespace-pre-wrap">{message.text}</p>

        {message.pending && message.statusLabel && (
          <p className="mt-2 animate-pulse text-xs text-neutral-400">{message.statusLabel}</p>
        )}

        {message.error && <p className="mt-2 text-xs text-red-400">{message.error}</p>}

        {!!message.toolCalls?.length && <ToolTrail toolCalls={message.toolCalls} />}

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
              <CitationChip key={c.id} citation={c} onClick={onCitationClick} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
