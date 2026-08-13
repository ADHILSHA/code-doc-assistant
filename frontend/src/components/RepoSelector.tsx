import { useEffect, useState } from "react";
import { createRepo, listRepos } from "../api/client";
import type { Repo } from "../types";

interface RepoSelectorProps {
  selectedRepoId: string | null;
  onRepoCreated: (repoId: string, jobId: string) => void;
  onRepoSelected: (repoId: string) => void;
  /** Bump this (e.g. on indexing completion) to force the repo list —
   * including each repo's status badge — to refetch. */
  refreshSignal?: unknown;
}

export function RepoSelector({
  selectedRepoId,
  onRepoCreated,
  onRepoSelected,
  refreshSignal,
}: RepoSelectorProps) {
  const [source, setSource] = useState("");
  const [repos, setRepos] = useState<Repo[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    refreshRepos();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal]);

  async function refreshRepos() {
    try {
      setRepos(await listRepos());
    } catch {
      // Non-fatal: the repo list is a convenience, not required to index a new one.
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!source.trim() || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const { repo_id, job_id } = await createRepo(source.trim());
      setSource("");
      onRepoCreated(repo_id, job_id);
      refreshRepos();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to create repo");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-neutral-800 bg-neutral-900 p-4">
      <form onSubmit={handleSubmit} className="flex gap-2">
        <input
          type="text"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          placeholder="GitHub URL…"
          className="flex-1 rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm
                     text-neutral-100 placeholder:text-neutral-500 focus:border-neutral-500
                     focus:outline-none"
        />
        <button
          type="submit"
          disabled={submitting || !source.trim()}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white
                     hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? "Indexing…" : "Index"}
        </button>
      </form>
      {error && <p className="text-sm text-red-400">{error}</p>}

      {repos.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="text-xs uppercase tracking-wide text-neutral-500">Repositories</p>
          <ul className="flex flex-col gap-1">
            {repos.map((repo) => (
              <li key={repo.id}>
                <button
                  onClick={() => onRepoSelected(repo.id)}
                  className={`flex w-full items-center justify-between rounded-md px-3 py-1.5
                              text-left text-sm hover:bg-neutral-800 ${
                                repo.id === selectedRepoId ? "bg-neutral-800" : ""
                              }`}
                >
                  <span className="truncate">{repo.display_name}</span>
                  <StatusBadge status={repo.status} />
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function StatusBadge({ status }: { status: Repo["status"] }) {
  const colors: Record<Repo["status"], string> = {
    pending: "text-neutral-400",
    cloning: "text-amber-400",
    parsing: "text-amber-400",
    embedding: "text-amber-400",
    ready: "text-emerald-400",
    failed: "text-red-400",
  };
  return <span className={`ml-2 shrink-0 text-xs ${colors[status]}`}>{status}</span>;
}
