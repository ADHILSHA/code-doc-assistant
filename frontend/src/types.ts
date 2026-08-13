// Mirrors backend/app/models.py — keep in sync with the API contract (SPEC.md §5).

export type RepoStatus = "pending" | "cloning" | "parsing" | "embedding" | "ready" | "failed";
export type SourceType = "github" | "local";
export type JobType = "index" | "reindex";
export type JobState = "queued" | "running" | "succeeded" | "failed";

export interface RepoStats {
  files: number;
  files_skipped: number;
  chunks: number;
  chunks_embedded: number;
  languages: string[];
  chunking?: Record<string, number>; // e.g. {"ast": 40, "markdown": 3, "naive": 2}
  fallback_languages?: string[]; // languages that fell back to the naive splitter
  symbols?: number; // Phase 2: rows written to `symbols`
  endpoints?: number; // Phase 2: rows written to `endpoints`
  dependencies?: number; // Phase 2: rows written to `dependencies`
}

export interface Repo {
  id: string;
  source: string;
  source_type: SourceType;
  display_name: string;
  status: RepoStatus;
  error: string | null;
  commit_sha: string | null;
  default_branch: string | null;
  stats: RepoStats | null;
  created_at: string;
  indexed_at: string | null;
}

export interface Job {
  id: string;
  repo_id: string;
  type: JobType;
  state: JobState;
  stage: string | null;
  progress: number;
  message: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceChunk {
  path: string;
  start_line: number;
  end_line: number;
  symbol: string | null;
}

export interface Citation {
  id: number;
  path: string;
  start_line: number;
  end_line: number;
  url: string | null;
}

export interface FileSlice {
  path: string;
  language: string | null;
  start_line: number;
  end_line: number;
  lines: string[];
}

// --- GET /repos/{id}/endpoints, GET /repos/{id}/dependencies (SPEC.md §6 Phase 2) ---

export interface Endpoint {
  method: string | null; // null = handles any method (e.g. Express `.all()`)
  route: string;
  framework: string | null;
  handler_symbol: string | null;
  file_path: string | null;
  line: number | null;
  auth_hint: string | null;
}

export interface Dependency {
  ecosystem: string | null;
  name: string;
  version_spec: string | null;
  kind: string | null;
  manifest_path: string;
}

export type ChatRole = "user" | "assistant";

// SPEC.md §5 Phase 3: emitted once per agent tool call, live while the
// agent loop runs — the frontend shows these as the agent's reasoning trail.
export interface ToolCallEvent {
  name: string;
  input: Record<string, unknown>;
  result_summary: string;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  sources?: SourceChunk[];
  citations?: Citation[];
  toolCalls?: ToolCallEvent[];
  statusLabel?: string; // last "status" event seen, shown while streaming
  pending?: boolean; // true until the "done"/"error" event arrives
  error?: string;
}
