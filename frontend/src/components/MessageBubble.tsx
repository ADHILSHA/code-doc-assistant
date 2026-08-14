import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatMessage, Citation } from "../types";
import { CitationChip } from "./CitationChip";
import { CodeBlock } from "./CodeBlock";
import { ToolTrail } from "./ToolTrail";

interface MessageBubbleProps {
  message: ChatMessage;
  onCitationClick: (citation: Citation) => void;
}

// SPEC.md §6 Phase 5 task 1 ("syntax highlighting" + general markdown
// rendering polish): the synthesis prompt asks the model for prose, and
// prose from an LLM routinely includes markdown formatting (lists, bold,
// inline code, the occasional fenced example) — rendering it as markdown
// instead of a flat `<p>` is a real readability improvement, not just
// decoration. Citation markers (`[path/to/file.py:120-145]`, verified by
// generation/citations.py) stay untouched by this: bare `[text]` with no
// following `(url)` isn't a markdown link, so it renders as literal
// bracketed text either way — the *separate* clickable CitationChip row
// below the text is still the only way to open a citation, unchanged from
// Phase 1.
function MarkdownBody({ text }: { text: string }) {
  return (
    <div
      className="prose prose-sm prose-invert max-w-none
                 prose-p:my-1.5 prose-headings:my-2 prose-ul:my-1.5 prose-ol:my-1.5
                 prose-pre:my-2 prose-pre:bg-neutral-950 prose-pre:border prose-pre:border-neutral-800
                 prose-code:before:content-none prose-code:after:content-none
                 prose-a:text-blue-400"
    >
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          code({ className, children, ...props }) {
            const match = /language-(\w+)/.exec(className ?? "");
            const code = String(children).replace(/\n$/, "");
            if (!match) {
              // Inline `code span` — no fence, no language.
              return (
                <code className="rounded bg-neutral-800 px-1 py-0.5 font-mono text-[0.85em]" {...props}>
                  {children}
                </code>
              );
            }
            return (
              <div className="overflow-x-auto rounded">
                <CodeBlock code={code} language={match[1]} />
              </div>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
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
        {isUser ? <p className="whitespace-pre-wrap">{message.text}</p> : <MarkdownBody text={message.text} />}

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
