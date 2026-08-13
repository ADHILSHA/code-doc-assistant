import type { Citation } from "../types";

interface CitationChipProps {
  citation: Citation;
  onClick: (citation: Citation) => void;
}

export function CitationChip({ citation, onClick }: CitationChipProps) {
  return (
    <button
      type="button"
      onClick={() => onClick(citation)}
      title={`Open ${citation.path}:${citation.start_line}-${citation.end_line}`}
      className="rounded border border-neutral-600 px-1.5 py-0.5 font-mono text-[11px] text-neutral-300
                 transition-colors hover:border-blue-500 hover:bg-neutral-800 hover:text-blue-400"
    >
      [{citation.path}:{citation.start_line}-{citation.end_line}]
    </button>
  );
}
