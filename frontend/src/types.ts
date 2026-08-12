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

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  sources?: SourceChunk[];
  citations?: Citation[];
  statusLabel?: string; // last "status" event seen, shown while streaming
  pending?: boolean; // true until the "done"/"error" event arrives
  error?: string;
}
