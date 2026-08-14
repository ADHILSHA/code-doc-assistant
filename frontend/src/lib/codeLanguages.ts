// Split out of components/CodeBlock.tsx (not a component itself) so that
// file only exports the component — keeps React Fast Refresh working
// there (oxlint's react/only-export-components rule).

export const SUPPORTED_LANGUAGES = new Set([
  "python", "javascript", "typescript", "tsx", "jsx", "go", "java", "rust",
  "ruby", "markdown", "json", "yaml", "css", "bash", "sql", "c", "cpp",
  "csharp", "php",
]);

const ALIASES: Record<string, string> = {
  py: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  sh: "bash",
  shell: "bash",
  zsh: "bash",
  yml: "yaml",
  "c++": "cpp",
  "c#": "csharp",
  cs: "csharp",
  md: "markdown",
};

/** Maps a language hint (from FileSlice.language, or a markdown fenced
 * code block's info string) onto a registered grammar name, tolerating the
 * aliases people/LLMs actually write ("py", "sh", "ts", "shell", ...).
 * Returns null for anything unrecognized so the caller can fall back to
 * plain (no highlighting, still monospaced) rather than crashing. */
export function normalizeLanguage(lang: string | null | undefined): string | null {
  if (!lang) return null;
  const key = lang.toLowerCase().trim();
  const resolved = ALIASES[key] ?? key;
  return SUPPORTED_LANGUAGES.has(resolved) ? resolved : null;
}
