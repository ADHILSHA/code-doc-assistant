// PrismLight (synchronous), not PrismAsyncLight: we already statically
// import + registerLanguage() every grammar we need below, so there's
// nothing to load lazily — PrismAsyncLight's internal module still
// references its *entire* async-language map (every Prism-supported
// language, ~250 of them) for the languages Vite's build graph reaches,
// which made `npm run build` emit a separate chunk file per unused
// language. Found by actually running the build, not by inspection.
import { PrismLight as SyntaxHighlighter } from "react-syntax-highlighter";
import bash from "react-syntax-highlighter/dist/esm/languages/prism/bash";
import c from "react-syntax-highlighter/dist/esm/languages/prism/c";
import cpp from "react-syntax-highlighter/dist/esm/languages/prism/cpp";
import csharp from "react-syntax-highlighter/dist/esm/languages/prism/csharp";
import css from "react-syntax-highlighter/dist/esm/languages/prism/css";
import go from "react-syntax-highlighter/dist/esm/languages/prism/go";
import java from "react-syntax-highlighter/dist/esm/languages/prism/java";
import javascript from "react-syntax-highlighter/dist/esm/languages/prism/javascript";
import json from "react-syntax-highlighter/dist/esm/languages/prism/json";
import jsx from "react-syntax-highlighter/dist/esm/languages/prism/jsx";
import markdown from "react-syntax-highlighter/dist/esm/languages/prism/markdown";
import php from "react-syntax-highlighter/dist/esm/languages/prism/php";
import python from "react-syntax-highlighter/dist/esm/languages/prism/python";
import ruby from "react-syntax-highlighter/dist/esm/languages/prism/ruby";
import rust from "react-syntax-highlighter/dist/esm/languages/prism/rust";
import sql from "react-syntax-highlighter/dist/esm/languages/prism/sql";
import tsx from "react-syntax-highlighter/dist/esm/languages/prism/tsx";
import typescript from "react-syntax-highlighter/dist/esm/languages/prism/typescript";
import yaml from "react-syntax-highlighter/dist/esm/languages/prism/yaml";
import { vscDarkPlus } from "react-syntax-highlighter/dist/esm/styles/prism";
import { normalizeLanguage } from "../lib/codeLanguages";

// Registered once, module-level — matches backend/app/parsing/languages.py's
// `_EXTENSION_LANGUAGE` (the `language` value a FileSlice/citation can
// carry) plus a handful of common non-source formats (json/yaml/css/sql/...)
// that get indexed but have no tree-sitter grammar server-side. `bash` is
// registered too since the assistant's prose (MessageBubble) often includes
// example shell commands the backend never tagged a language for.
SyntaxHighlighter.registerLanguage("python", python);
SyntaxHighlighter.registerLanguage("javascript", javascript);
SyntaxHighlighter.registerLanguage("typescript", typescript);
SyntaxHighlighter.registerLanguage("tsx", tsx);
SyntaxHighlighter.registerLanguage("jsx", jsx);
SyntaxHighlighter.registerLanguage("go", go);
SyntaxHighlighter.registerLanguage("java", java);
SyntaxHighlighter.registerLanguage("rust", rust);
SyntaxHighlighter.registerLanguage("ruby", ruby);
SyntaxHighlighter.registerLanguage("markdown", markdown);
SyntaxHighlighter.registerLanguage("json", json);
SyntaxHighlighter.registerLanguage("yaml", yaml);
SyntaxHighlighter.registerLanguage("css", css);
SyntaxHighlighter.registerLanguage("bash", bash);
SyntaxHighlighter.registerLanguage("sql", sql);
SyntaxHighlighter.registerLanguage("c", c);
SyntaxHighlighter.registerLanguage("cpp", cpp);
SyntaxHighlighter.registerLanguage("csharp", csharp);
SyntaxHighlighter.registerLanguage("php", php);

interface CodeBlockProps {
  code: string;
  language: string | null | undefined;
  /** Line numbers shown in the gutter, starting from this value —
   * omit for message-bubble code fences (no meaningful starting line). */
  startingLineNumber?: number;
  /** 1-indexed [start, end] inclusive range to highlight (CodeViewer's
   * "this is the cited range" background) — omit for no highlighting. */
  highlightRange?: [number, number];
}

export function CodeBlock({ code, language, startingLineNumber, highlightRange }: CodeBlockProps) {
  const resolved = normalizeLanguage(language);
  return (
    <SyntaxHighlighter
      language={resolved ?? "text"}
      style={vscDarkPlus}
      showLineNumbers={startingLineNumber !== undefined}
      startingLineNumber={startingLineNumber}
      wrapLines={highlightRange !== undefined}
      lineProps={
        highlightRange
          ? (lineNumber: number) => {
              const inRange = lineNumber >= highlightRange[0] && lineNumber <= highlightRange[1];
              return { className: inRange ? "bg-blue-500/15 -mx-4 px-4 block" : "block" };
            }
          : undefined
      }
      customStyle={{ margin: 0, background: "transparent", fontSize: "0.75rem" }}
      codeTagProps={{ className: "font-mono" }}
    >
      {code}
    </SyntaxHighlighter>
  );
}
