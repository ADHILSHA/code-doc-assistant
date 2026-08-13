import { useState } from "react";
import type { ToolCallEvent } from "../types";

interface ToolTrailProps {
  toolCalls: ToolCallEvent[];
}

const TOOL_ICONS: Record<string, string> = {
  semantic_search: "🔎",
  grep: "🔍",
  find_files: "📁",
  read_file: "📄",
  get_definition: "🎯",
  find_references: "🔗",
  list_directory: "🗂️",
  get_dependencies: "📦",
  list_endpoints: "🌐",
  get_summary: "📝",
};

function formatInput(input: Record<string, unknown>): string {
  const entries = Object.entries(input);
  if (entries.length === 0) return "";
  return entries.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
}

// SPEC.md §6 Phase 3 acceptance criterion: "Tool trail is visible in the
// UI." Collapsed by default (an agent that ran several tools shouldn't
// dominate the message), but every call — live, as SSE `tool` events
// arrive — is listed underneath.
export function ToolTrail({ toolCalls }: ToolTrailProps) {
  const [expanded, setExpanded] = useState(false);
  if (toolCalls.length === 0) return null;

  return (
    <div className="mt-3 border-t border-neutral-700 pt-2">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex items-center gap-1 text-xs text-neutral-400 hover:text-neutral-200"
      >
        <span>{expanded ? "▾" : "▸"}</span>
        <span>
          {toolCalls.length} tool call{toolCalls.length === 1 ? "" : "s"}
        </span>
      </button>
      {expanded && (
        <ul className="mt-1.5 flex flex-col gap-1">
          {toolCalls.map((call, i) => (
            <li key={i} className="rounded bg-neutral-900 px-2 py-1 text-[11px] text-neutral-400">
              <span className="mr-1">{TOOL_ICONS[call.name] ?? "🛠️"}</span>
              <span className="font-mono text-neutral-300">{call.name}</span>
              {formatInput(call.input) && (
                <span className="text-neutral-500">({formatInput(call.input)})</span>
              )}
              <span className="ml-1 text-neutral-500">→ {call.result_summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
