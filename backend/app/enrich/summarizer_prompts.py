"""Named prompt constants for hierarchical summarization (SPEC.md §7.6)."""

from __future__ import annotations

FILE_SUMMARY_SYSTEM_PROMPT = """You summarize a single source file for a code documentation assistant. \
In 2-4 sentences, describe what this file is for and its main responsibilities — not a line-by-line \
walkthrough. Mention key classes/functions by name if they're central to the file's purpose. Output \
only the summary, no preamble."""


def build_file_summary_user_message(path: str, content: str) -> str:
    return f"File: {path}\n\n{content}"


DIRECTORY_SUMMARY_SYSTEM_PROMPT = """You summarize a directory for a code documentation assistant, \
given summaries of its immediate contents (files and subdirectories). In 2-4 sentences, describe what \
this part of the codebase is responsible for, synthesizing across its contents rather than listing them \
one by one. Output only the summary, no preamble."""


def build_directory_summary_user_message(
    directory: str, child_files: dict[str, str], child_dirs: dict[str, str]
) -> str:
    parts = [f"Directory: {directory}"]
    for path, summary in child_files.items():
        parts.append(f"- file {path}: {summary}")
    for path, summary in child_dirs.items():
        parts.append(f"- subdirectory {path}/: {summary}")
    return "\n".join(parts)


REPO_SUMMARY_SYSTEM_PROMPT = """You summarize an entire codebase for a code documentation assistant, \
given summaries of its top-level files and directories. In 3-6 sentences, describe what the project is, \
what it does, and how it's organized at a high level — the kind of answer a new contributor's first \
question deserves. Output only the summary, no preamble."""


def build_repo_summary_user_message(
    display_name: str, top_dirs: dict[str, str], top_files: dict[str, str]
) -> str:
    parts = [f"Repository: {display_name}"]
    for path, summary in top_dirs.items():
        parts.append(f"- {path}/: {summary}")
    for path, summary in top_files.items():
        parts.append(f"- {path}: {summary}")
    return "\n".join(parts)
