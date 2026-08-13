// Thin fetch wrappers over the backend API (SPEC.md §5). No business logic
// here — the "no business logic in api handlers" rule applies to the
// frontend's API layer too.

import type { Citation, FileSlice, Job, Repo, SourceChunk } from "../types";

const BASE = "/api";

async function extractError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    return body.detail ?? `request failed: ${res.status}`;
  } catch {
    return `request failed: ${res.status}`;
  }
}

export async function createRepo(source: string): Promise<{ repo_id: string; job_id: string }> {
  const res = await fetch(`${BASE}/repos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  if (!res.ok) throw new Error(await extractError(res));
  return res.json();
}

export async function listRepos(): Promise<Repo[]> {
  const res = await fetch(`${BASE}/repos`);
  if (!res.ok) throw new Error(await extractError(res));
  return res.json();
}

export async function getRepo(id: string): Promise<Repo> {
  const res = await fetch(`${BASE}/repos/${id}`);
  if (!res.ok) throw new Error(await extractError(res));
  return res.json();
}

export async function deleteRepo(id: string): Promise<void> {
  const res = await fetch(`${BASE}/repos/${id}`, { method: "DELETE" });
  if (!res.ok) throw new Error(await extractError(res));
}

export async function getJob(id: string): Promise<Job> {
  const res = await fetch(`${BASE}/jobs/${id}`);
  if (!res.ok) throw new Error(await extractError(res));
  return res.json();
}

export async function getFileSlice(
  repoId: string,
  path: string,
  start?: number,
  end?: number,
): Promise<FileSlice> {
  const params = new URLSearchParams({ path });
  if (start !== undefined) params.set("start", String(start));
  if (end !== undefined) params.set("end", String(end));
  const res = await fetch(`${BASE}/repos/${repoId}/file?${params.toString()}`);
  if (!res.ok) throw new Error(await extractError(res));
  return res.json();
}

// --- SSE streaming for POST /api/query ---
//
// The browser's EventSource API can't send a POST body, so we parse
// `text/event-stream` by hand off a streamed fetch() response.

interface RawSSEEvent {
  event: string;
  data: string;
}

async function* parseSSEStream(response: Response): AsyncGenerator<RawSSEEvent> {
  if (!response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    buffer = buffer.replace(/\r\n/g, "\n");

    let sepIndex: number;
    while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
      const raw = buffer.slice(0, sepIndex);
      buffer = buffer.slice(sepIndex + 2);
      const parsed = parseRawEvent(raw);
      if (parsed) yield parsed;
    }
  }
}

function parseRawEvent(raw: string): RawSSEEvent | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of raw.split("\n")) {
    if (line.startsWith("event:")) eventName = line.slice("event:".length).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice("data:".length).trim());
  }
  if (dataLines.length === 0) return null;
  return { event: eventName, data: dataLines.join("\n") };
}

export interface QueryHandlers {
  onStatus?: (stage: string, detail: string) => void;
  onSources?: (chunks: SourceChunk[]) => void;
  onToken?: (text: string) => void;
  onCitations?: (citations: Citation[]) => void;
  onDone?: (queryId: number, latencyMs: number, route: string) => void;
  onError?: (message: string) => void;
}

export async function streamQuery(
  repoId: string,
  question: string,
  handlers: QueryHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/query`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_id: repoId, question }),
      signal,
    });
  } catch (err) {
    handlers.onError?.(err instanceof Error ? err.message : "network error");
    return;
  }

  if (!response.ok) {
    handlers.onError?.(await extractError(response));
    return;
  }

  for await (const evt of parseSSEStream(response)) {
    let payload: any;
    try {
      payload = JSON.parse(evt.data);
    } catch {
      continue;
    }
    switch (evt.event) {
      case "status":
        handlers.onStatus?.(payload.stage, payload.detail);
        break;
      case "sources":
        handlers.onSources?.(payload.chunks);
        break;
      case "token":
        handlers.onToken?.(payload.text);
        break;
      case "citations":
        handlers.onCitations?.(payload.citations);
        break;
      case "done":
        handlers.onDone?.(payload.query_id, payload.latency_ms, payload.route);
        break;
      case "error":
        handlers.onError?.(payload.message);
        break;
    }
  }
}
