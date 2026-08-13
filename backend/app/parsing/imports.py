"""Best-effort import extraction, for chunk header enrichment only.

SPEC.md §6 Phase 1 task 1's header format is `# path/to/file.py | class
UserService | imports: fastapi, sqlalchemy`. This is NOT the real import
graph — that's Phase 2's `parsing/refs.py` (`import_edges` table, resolved
against `files`). This is deliberately a few lines of regex per language,
capped and scanning only the first ~2000 chars, used solely to give the
embedding model a little more context about what ecosystem a chunk's code
belongs to.
"""

from __future__ import annotations

import re

_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^(?:from\s+(\S+)\s+import|import\s+(\S+))", re.MULTILINE),
    "javascript": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    "typescript": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    "tsx": re.compile(r"""from\s+["'](\S+?)["']|require\(\s*["'](\S+?)["']\s*\)"""),
    # Only lines that are just a quoted import path (optionally prefixed
    # `import `) — matches both `import "fmt"` and the lines inside an
    # `import (...)` block, without matching arbitrary string literals.
    "go": re.compile(r'^\s*(?:import\s+)?"([\w./-]+)"\s*$', re.MULTILINE),
    "java": re.compile(r"^import\s+(?:static\s+)?([\w.]+);", re.MULTILINE),
    "rust": re.compile(r"^use\s+([\w:]+)", re.MULTILINE),
    "ruby": re.compile(r"""require(?:_relative)?\s*["'](\S+?)["']"""),
}

_MAX_IMPORTS = 6
_SCAN_CHARS = 2000


def extract_imports(text: str, language: str | None) -> list[str]:
    """Capped, deduplicated, best-effort list of imported module names near
    the top of the file. Returns [] for languages without a pattern above."""
    pattern = _PATTERNS.get(language or "")
    if pattern is None:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text[:_SCAN_CHARS]):
        name = next((g for g in match.groups() if g), None)
        if name and name not in seen:
            seen.add(name)
            names.append(name)
            if len(names) >= _MAX_IMPORTS:
                break
    return names
