import { useState } from "react";
import { getRepo } from "./api/client";
import { ChatPanel } from "./components/ChatPanel";
import { CodeViewer } from "./components/CodeViewer";
import { IndexProgress } from "./components/IndexProgress";
import { RepoSelector } from "./components/RepoSelector";
import type { Citation } from "./types";

type ViewState =
  | { kind: "idle" }
  | { kind: "indexing"; repoId: string; jobId: string }
  | { kind: "not-ready"; repoId: string; status: string }
  | { kind: "ready"; repoId: string };

function selectedRepoId(view: ViewState): string | null {
  return view.kind === "idle" ? null : view.repoId;
}

function App() {
  const [view, setView] = useState<ViewState>({ kind: "idle" });
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);

  async function handleRepoSelected(repoId: string) {
    try {
      const repo = await getRepo(repoId);
      setView(
        repo.status === "ready"
          ? { kind: "ready", repoId }
          : { kind: "not-ready", repoId, status: repo.status },
      );
    } catch {
      // Selecting from the list is a convenience; a failed lookup just
      // leaves the current view unchanged.
    }
    setSelectedCitation(null);
  }

  const showCodeViewer = view.kind === "ready" && selectedCitation !== null;

  return (
    <div
      className={`mx-auto flex h-screen flex-col gap-4 p-6 transition-[max-width] ${
        showCodeViewer ? "max-w-7xl" : "max-w-4xl"
      }`}
    >
      <header>
        <h1 className="text-lg font-semibold text-neutral-100">Code Documentation Assistant</h1>
        <p className="text-sm text-neutral-500">
          Point this at a repo and ask it how the code works.
        </p>
      </header>

      <RepoSelector
        selectedRepoId={selectedRepoId(view)}
        onRepoCreated={(repoId, jobId) => setView({ kind: "indexing", repoId, jobId })}
        onRepoSelected={handleRepoSelected}
        refreshSignal={view.kind === "ready" ? view.repoId : undefined}
      />

      {view.kind === "indexing" && (
        <IndexProgress
          jobId={view.jobId}
          onComplete={() => setView({ kind: "ready", repoId: view.repoId })}
        />
      )}

      {view.kind === "not-ready" && (
        <p className="text-sm text-amber-400">
          This repo is still {view.status}. Try selecting it again in a moment.
        </p>
      )}

      {view.kind === "ready" && (
        <div className="flex min-h-0 flex-1 gap-4">
          <div className="min-h-0 min-w-0 flex-1">
            <ChatPanel repoId={view.repoId} onCitationClick={setSelectedCitation} />
          </div>
          {showCodeViewer && selectedCitation && (
            <div className="min-h-0 w-[45%] shrink-0">
              <CodeViewer
                repoId={view.repoId}
                citation={selectedCitation}
                onClose={() => setSelectedCitation(null)}
              />
            </div>
          )}
        </div>
      )}

      {view.kind === "idle" && (
        <p className="text-sm text-neutral-500">Index a repo above to start asking questions.</p>
      )}
    </div>
  );
}

export default App;
