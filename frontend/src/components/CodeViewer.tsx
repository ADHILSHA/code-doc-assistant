import { useEffect, useState } from "react";
import { getFileSlice } from "../api/client";
import type { Citation, FileSlice } from "../types";
import { CodeBlock } from "./CodeBlock";

// Lines of context fetched around the cited range, so the viewer doesn't
// show the citation as an isolated snippet with no surrounding signature/
// braces. The backend just returns "lines N..M of this file" — the
// padding + highlight-the-exact-range logic lives here (SPEC.md §6 Phase 1
// task 5).
const CONTEXT_PADDING = 8;

interface CodeViewerProps {
  repoId: string;
  citation: Citation;
  onClose: () => void;
}

export function CodeViewer({ repoId, citation, onClose }: CodeViewerProps) {
  const [slice, setSlice] = useState<FileSlice | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSlice(null);

    const paddedStart = Math.max(1, citation.start_line - CONTEXT_PADDING);
    const paddedEnd = citation.end_line + CONTEXT_PADDING;
    getFileSlice(repoId, citation.path, paddedStart, paddedEnd)
      .then((s) => {
        if (!cancelled) setSlice(s);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "failed to load file");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [repoId, citation.path, citation.start_line, citation.end_line]);

  // Escape closes the viewer, matching the "click ✕" affordance — SPEC.md
  // §6 Phase 5 task 1 keyboard-nav polish. Scoped to this component's
  // lifetime (the viewer only exists while a citation is open), not a
  // global listener that would need its own enable/disable bookkeeping.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="flex h-full flex-col rounded-lg border border-neutral-800 bg-neutral-950">
      <div className="flex items-center justify-between border-b border-neutral-800 px-4 py-2">
        <span className="truncate font-mono text-sm text-neutral-300">
          {citation.path}
          <span className="text-neutral-500">
            :{citation.start_line}-{citation.end_line}
          </span>
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close (Esc)"
          title="Close (Esc)"
          className="shrink-0 text-neutral-500 hover:text-neutral-200"
        >
          ✕
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {loading && <p className="p-4 text-sm text-neutral-500">Loading…</p>}
        {error && <p className="p-4 text-sm text-red-400">{error}</p>}
        {slice && slice.lines.length === 0 && (
          <p className="p-4 text-sm text-neutral-500">This file has no content in the requested range.</p>
        )}
        {slice && slice.lines.length > 0 && (
          <CodeBlock
            code={slice.lines.join("\n")}
            language={slice.language}
            startingLineNumber={slice.start_line}
            highlightRange={[citation.start_line, citation.end_line]}
          />
        )}
      </div>

      {citation.url && (
        <div className="border-t border-neutral-800 px-4 py-2">
          <a
            href={citation.url}
            target="_blank"
            rel="noreferrer"
            className="text-xs text-blue-400 hover:underline"
          >
            View on GitHub ↗
          </a>
        </div>
      )}
    </div>
  );
}
