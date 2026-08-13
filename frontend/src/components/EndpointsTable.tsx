import { useEffect, useState } from "react";
import { getEndpoints } from "../api/client";
import type { Citation, Endpoint } from "../types";

interface EndpointsTableProps {
  repoId: string;
  onOpenCitation: (citation: Citation) => void;
}

const METHOD_COLORS: Record<string, string> = {
  GET: "text-emerald-400 border-emerald-800",
  POST: "text-blue-400 border-blue-800",
  PUT: "text-amber-400 border-amber-800",
  PATCH: "text-amber-400 border-amber-800",
  DELETE: "text-red-400 border-red-800",
};

function methodBadge(method: string | null) {
  const label = method ?? "ANY";
  const colors = METHOD_COLORS[label] ?? "text-neutral-400 border-neutral-700";
  return (
    <span className={`inline-block w-16 shrink-0 rounded border px-1.5 py-0.5 text-center font-mono text-[11px] ${colors}`}>
      {label}
    </span>
  );
}

export function EndpointsTable({ repoId, onOpenCitation }: EndpointsTableProps) {
  const [endpoints, setEndpoints] = useState<Endpoint[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEndpoints(null);
    setError(null);
    getEndpoints(repoId)
      .then((rows) => {
        if (!cancelled) setEndpoints(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "failed to load endpoints");
      });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  if (error) return <p className="p-4 text-sm text-red-400">{error}</p>;
  if (!endpoints) return <p className="p-4 text-sm text-neutral-500">Loading…</p>;
  if (endpoints.length === 0) {
    return <p className="p-4 text-sm text-neutral-500">No API endpoints were found in this repo.</p>;
  }

  return (
    <div className="flex h-full flex-col overflow-auto rounded-lg border border-neutral-800 bg-neutral-950">
      <div className="border-b border-neutral-800 px-4 py-2 text-xs text-neutral-500">
        {endpoints.length} endpoint{endpoints.length === 1 ? "" : "s"}
      </div>
      <ul className="divide-y divide-neutral-900">
        {endpoints.map((e, i) => (
          <li key={i} className="flex items-center gap-3 px-4 py-2 text-sm">
            {methodBadge(e.method)}
            <span className="min-w-0 flex-1 truncate font-mono text-neutral-200">{e.route}</span>
            {e.handler_symbol && (
              <span className="shrink-0 truncate font-mono text-xs text-neutral-500">{e.handler_symbol}</span>
            )}
            {e.auth_hint && (
              <span
                title={`requires auth: ${e.auth_hint}`}
                className="shrink-0 rounded border border-amber-800 px-1.5 py-0.5 text-[10px] text-amber-400"
              >
                🔒 auth
              </span>
            )}
            {e.file_path && e.line ? (
              <button
                type="button"
                onClick={() =>
                  onOpenCitation({ id: i, path: e.file_path as string, start_line: e.line as number, end_line: e.line as number, url: null })
                }
                className="shrink-0 rounded border border-neutral-700 px-1.5 py-0.5 font-mono text-[11px] text-neutral-400
                           transition-colors hover:border-blue-500 hover:text-blue-400"
              >
                {e.file_path}:{e.line}
              </button>
            ) : (
              e.framework === "openapi" && (
                <span className="shrink-0 text-[11px] text-neutral-600">from spec</span>
              )
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
