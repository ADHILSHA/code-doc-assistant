import { useEffect, useState } from "react";
import { getFileSlice } from "../api/client";
import type { Citation, FileSlice } from "../types";

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
          aria-label="Close"
          className="shrink-0 text-neutral-500 hover:text-neutral-200"
        >
          ✕
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {loading && <p className="p-4 text-sm text-neutral-500">Loading…</p>}
        {error && <p className="p-4 text-sm text-red-400">{error}</p>}
        {slice && (
          <div className="py-2 font-mono text-xs leading-5">
            {slice.lines.map((line, i) => {
              const lineNumber = slice.start_line + i;
              const highlighted = lineNumber >= citation.start_line && lineNumber <= citation.end_line;
              return (
                <div key={lineNumber} className={`flex px-2 ${highlighted ? "bg-blue-500/15" : ""}`}>
                  <span className="mr-4 w-10 shrink-0 select-none text-right text-neutral-600">
                    {lineNumber}
                  </span>
                  <code className="whitespace-pre text-neutral-200">{line}</code>
                </div>
              );
            })}
          </div>
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
