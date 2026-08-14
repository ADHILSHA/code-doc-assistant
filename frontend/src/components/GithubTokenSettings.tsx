import { useEffect, useState } from "react";
import { deleteGithubToken, getGithubTokenStatus, setGithubToken } from "../api/client";

// SPEC.md §6 Phase 5 task 2: PAT entry UI. A small popover, not a route —
// this is a one-off setup action, not something that needs its own place
// in the tab bar. The token is a write-only value here: the input is
// always empty on open (there's nothing to prefill — GET only ever
// returns `{configured: bool}`, never the token itself, matching the
// backend's "never log or return it via the API" contract), and it's
// cleared from the input immediately after a successful save.
export function GithubTokenSettings() {
  const [open, setOpen] = useState(false);
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setError(null);
    getGithubTokenStatus()
      .then((s) => setConfigured(s.configured))
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load status"));
  }, [open]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    if (open) window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open]);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await setGithubToken(token.trim());
      setConfigured(true);
      setToken("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to save token");
    } finally {
      setBusy(false);
    }
  }

  async function handleClear() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteGithubToken();
      setConfigured(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to clear token");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title="GitHub access token (for private repos)"
        aria-label="GitHub access token settings"
        className="rounded-md border border-neutral-800 p-1.5 text-neutral-400 hover:border-neutral-600 hover:text-neutral-200"
      >
        🔑
      </button>

      {open && (
        <>
          {/* Click-outside-to-close backdrop. */}
          <button
            type="button"
            aria-hidden="true"
            tabIndex={-1}
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-10 cursor-default"
          />
          <div className="absolute right-0 z-20 mt-2 w-80 rounded-lg border border-neutral-700 bg-neutral-900 p-4 shadow-xl">
            <h2 className="text-sm font-medium text-neutral-100">GitHub access token</h2>
            <p className="mt-1 text-xs text-neutral-500">
              Needed only to index <span className="text-neutral-400">private</span> repositories.
              Stored encrypted at rest; never shown again once saved.
            </p>

            <div className="mt-3 flex items-center gap-2 text-xs">
              <span
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                  configured ? "bg-emerald-500" : "bg-neutral-600"
                }`}
              />
              <span className="text-neutral-400">
                {configured === null ? "Checking…" : configured ? "A token is configured" : "No token configured"}
              </span>
            </div>

            <form onSubmit={handleSave} className="mt-3 flex flex-col gap-2">
              <input
                type="password"
                autoComplete="off"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="ghp_…"
                disabled={busy}
                className="rounded-md border border-neutral-700 bg-neutral-950 px-3 py-1.5 text-sm
                           text-neutral-100 placeholder:text-neutral-600 focus:border-neutral-500
                           focus:outline-none disabled:opacity-50"
              />
              <div className="flex gap-2">
                <button
                  type="submit"
                  disabled={busy || !token.trim()}
                  className="flex-1 rounded-md bg-blue-600 px-3 py-1.5 text-xs font-medium text-white
                             hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Save
                </button>
                {configured && (
                  <button
                    type="button"
                    onClick={handleClear}
                    disabled={busy}
                    className="rounded-md border border-neutral-700 px-3 py-1.5 text-xs text-neutral-300
                               hover:border-red-800 hover:text-red-400 disabled:opacity-50"
                  >
                    Remove
                  </button>
                )}
              </div>
            </form>

            {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
          </div>
        </>
      )}
    </div>
  );
}
