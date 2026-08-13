import { useEffect, useState } from "react";
import { getDependencies } from "../api/client";
import type { Dependency } from "../types";

interface DependencyListProps {
  repoId: string;
}

const KIND_COLORS: Record<string, string> = {
  runtime: "text-emerald-400 border-emerald-800",
  dev: "text-neutral-400 border-neutral-700",
  peer: "text-blue-400 border-blue-800",
  optional: "text-amber-400 border-amber-800",
};

export function DependencyList({ repoId }: DependencyListProps) {
  const [deps, setDeps] = useState<Dependency[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDeps(null);
    setError(null);
    getDependencies(repoId)
      .then((rows) => {
        if (!cancelled) setDeps(rows);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "failed to load dependencies");
      });
    return () => {
      cancelled = true;
    };
  }, [repoId]);

  if (error) return <p className="p-4 text-sm text-red-400">{error}</p>;
  if (!deps) return <p className="p-4 text-sm text-neutral-500">Loading…</p>;
  if (deps.length === 0) {
    return <p className="p-4 text-sm text-neutral-500">No dependency manifest was found in this repo.</p>;
  }

  const byManifest = new Map<string, Dependency[]>();
  for (const d of deps) {
    const list = byManifest.get(d.manifest_path) ?? [];
    list.push(d);
    byManifest.set(d.manifest_path, list);
  }

  return (
    <div className="flex h-full flex-col overflow-auto rounded-lg border border-neutral-800 bg-neutral-950">
      <div className="border-b border-neutral-800 px-4 py-2 text-xs text-neutral-500">
        {deps.length} dependenc{deps.length === 1 ? "y" : "ies"} across {byManifest.size} manifest
        {byManifest.size === 1 ? "" : "s"}
      </div>
      {[...byManifest.entries()].map(([manifestPath, rows]) => (
        <div key={manifestPath} className="border-b border-neutral-900 last:border-b-0">
          <div className="px-4 pt-3 pb-1 font-mono text-xs text-neutral-500">{manifestPath}</div>
          <ul className="divide-y divide-neutral-900">
            {rows
              .slice()
              .sort((a, b) => a.name.localeCompare(b.name))
              .map((d) => (
                <li key={`${d.manifest_path}:${d.name}`} className="flex items-center gap-3 px-4 py-1.5 text-sm">
                  <span className="min-w-0 flex-1 truncate font-mono text-neutral-200">{d.name}</span>
                  {d.version_spec && (
                    <span className="shrink-0 font-mono text-xs text-neutral-500">{d.version_spec}</span>
                  )}
                  {d.kind && (
                    <span
                      className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] ${
                        KIND_COLORS[d.kind] ?? "text-neutral-400 border-neutral-700"
                      }`}
                    >
                      {d.kind}
                    </span>
                  )}
                </li>
              ))}
          </ul>
        </div>
      ))}
    </div>
  );
}
