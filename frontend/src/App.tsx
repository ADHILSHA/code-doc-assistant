import { useState } from "react";
import { getRepo } from "./api/client";
import { ChatPanel } from "./components/ChatPanel";
import { CodeViewer } from "./components/CodeViewer";
import { DependencyList } from "./components/DependencyList";
import { EndpointsTable } from "./components/EndpointsTable";
import { IndexProgress } from "./components/IndexProgress";
import { RepoSelector } from "./components/RepoSelector";
import type { Citation } from "./types";

type ViewState =
  | { kind: "idle" }
  | { kind: "indexing"; repoId: string; jobId: string }
  | { kind: "not-ready"; repoId: string; status: string }
  | { kind: "ready"; repoId: string };

// SPEC.md §6 Phase 2: dedicated tabs for the two structured routes that
// also have their own GET endpoints (browse.py), alongside the chat.
type Tab = "chat" | "endpoints" | "dependencies";

function selectedRepoId(view: ViewState): string | null {
  return view.kind === "idle" ? null : view.repoId;
}

function App() {
  const [view, setView] = useState<ViewState>({ kind: "idle" });
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [tab, setTab] = useState<Tab>("chat");

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
    setTab("chat");
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
        <div className="flex min-h-0 flex-1 flex-col gap-3">
          <nav className="flex gap-1 border-b border-neutral-800">
            {(
              [
                ["chat", "Chat"],
                ["endpoints", "Endpoints"],
                ["dependencies", "Dependencies"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={`px-3 py-1.5 text-sm transition-colors ${
                  tab === key
                    ? "border-b-2 border-blue-500 text-neutral-100"
                    : "text-neutral-500 hover:text-neutral-300"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>

          <div className="flex min-h-0 flex-1 gap-4">
            <div className="min-h-0 min-w-0 flex-1">
              {tab === "chat" && (
                <ChatPanel repoId={view.repoId} onCitationClick={setSelectedCitation} />
              )}
              {tab === "endpoints" && (
                <EndpointsTable repoId={view.repoId} onOpenCitation={setSelectedCitation} />
              )}
              {tab === "dependencies" && <DependencyList repoId={view.repoId} />}
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
        </div>
      )}

      {view.kind === "idle" && (
        <p className="text-sm text-neutral-500">Index a repo above to start asking questions.</p>
      )}
    </div>
  );
}

export default App;
