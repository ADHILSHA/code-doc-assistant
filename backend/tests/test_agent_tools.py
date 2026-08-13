"""Agent tool implementations (SPEC.md §6 Phase 3 task 3), against the real
indexed mini_repo fixture. Path-traversal rejection is tested explicitly
per this phase's acceptance criteria.
"""

from __future__ import annotations

from pathlib import Path

from app import jobs
from app.agent.tools import TOOL_HANDLERS, TOOL_SPECS, ToolContext, execute_tool
from app.db import get_registry_connection, get_repo_connection
from app.providers.embeddings import FakeEmbeddingProvider

from .conftest import MINI_REPO, make_settings


def _tool_context(tmp_path: Path) -> ToolContext:
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    return ToolContext(
        conn=conn, repo_root=Path(local_path), embedding_provider=FakeEmbeddingProvider(), settings=settings
    )


def test_every_tool_spec_has_a_registered_handler():
    spec_names = {s["name"] for s in TOOL_SPECS}
    assert spec_names == set(TOOL_HANDLERS.keys())


def test_semantic_search_returns_relevant_chunk(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("semantic_search", ctx, {"query": "hash the password", "k": 5})
    assert any(r["path"] == "src/auth/auth.py" for r in result)
    assert "result" in summary


def test_grep_finds_known_identifier(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("grep", ctx, {"pattern": "hash_password"})
    assert any(m["path"] == "src/auth/auth.py" for m in result)
    assert all(m["line"] > 0 for m in result)


def test_grep_respects_glob_filter(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("grep", ctx, {"pattern": "function", "glob": "*.ts"})
    assert result
    assert all(m["path"].endswith((".ts", ".tsx")) for m in result)


def test_grep_respects_max_results_cap(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("grep", ctx, {"pattern": "field_", "max_results": 5})
    assert len(result) <= 5


def test_find_files_matches_glob(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("find_files", ctx, {"glob": "src/api/*.py"})
    assert set(result) == {"src/api/__init__.py", "src/api/fastapi_routes.py", "src/api/flask_routes.py"}


def test_read_file_returns_requested_slice(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("read_file", ctx, {"path": "src/auth/auth.py", "start_line": 1, "end_line": 3})
    assert result["start_line"] == 1
    assert result["end_line"] == 3
    assert len(result["lines"]) == 3
    assert "auth.py:1-3" in summary


def test_read_file_caps_at_agent_read_file_max_lines(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    ctx.settings.agent_read_file_max_lines = 5
    result, _summary = execute_tool("read_file", ctx, {"path": "src/big_handler.py", "start_line": 1})
    assert result["end_line"] - result["start_line"] + 1 <= 5


def test_read_file_nonexistent_file_returns_error_not_exception(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("read_file", ctx, {"path": "does/not/exist.py"})
    assert "error" in result
    assert "not found" in summary


# --- path traversal (explicit acceptance criterion) ---


def test_read_file_rejects_path_traversal(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    for evil_path in ["../../etc/passwd", "../../../etc/passwd", "src/../../../etc/passwd", "/etc/passwd"]:
        result, summary = execute_tool("read_file", ctx, {"path": evil_path})
        assert "error" in result, f"{evil_path!r} should have been rejected"
        assert "rejected" in summary


def test_grep_python_fallback_stays_inside_repo_root(tmp_path: Path):
    # Force the Python fallback path (as if ripgrep weren't installed) and
    # confirm it never reads outside repo_root even via a symlink-y file
    # list — it only ever reads paths already recorded in `files`, each
    # individually safe_join'd.
    from app.agent import tools as tools_module

    ctx = _tool_context(tmp_path)
    result = tools_module._grep_python_fallback(ctx, "password", None, 50)
    for match in result:
        assert not match["path"].startswith("..")
        assert not match["path"].startswith("/")


def test_unknown_tool_returns_error_not_exception(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("delete_everything", ctx, {})
    assert "error" in result
    assert "unknown tool" in summary


def test_get_definition_tool_matches_structured_query(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("get_definition", ctx, {"symbol": "UserService"})
    assert len(result) == 1
    assert result[0]["file_path"] == "src/users/service.py"
    assert "1 definition" in summary


def test_get_definition_unknown_symbol_returns_empty_list(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("get_definition", ctx, {"symbol": "NoSuchSymbol"})
    assert result == []


def test_find_references_tool(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("find_references", ctx, {"symbol": "get_user_by_id"})
    assert {r["file_path"] for r in result} == {"tests/test_service.py"}


def test_list_directory_tool_root(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, _summary = execute_tool("list_directory", ctx, {"path": ""})
    assert set(result["directories"]) == {"src", "tests", "web"}


def test_get_dependencies_tool_matches_manifest_count(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("get_dependencies", ctx, {})
    assert len(result) == 9
    assert "9 dependencies" in summary


def test_list_endpoints_tool_matches_ground_truth_count(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("list_endpoints", ctx, {})
    assert len(result) == 9
    assert "9 endpoint(s)" in summary


def test_get_summary_tool_no_summaries_yet_returns_error_not_exception(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    result, summary = execute_tool("get_summary", ctx, {"path": "."})
    assert "error" in result
    assert "no summary" in summary


def test_get_summary_tool_after_summarization(tmp_path: Path):
    from app.enrich.summarizer import summarize_repo
    from app.providers.llm import FakeLLMProvider

    ctx = _tool_context(tmp_path)
    summarize_repo(ctx.conn, FakeLLMProvider(), min_loc=5, display_name="mini_repo")

    result, summary = execute_tool("get_summary", ctx, {"path": "."})
    assert result["scope"] == "repo"
    assert result["content"]
    assert "repo summary" in summary


def test_tool_execution_never_raises_even_on_bad_args(tmp_path: Path):
    ctx = _tool_context(tmp_path)
    # Missing a required arg should surface as a tool-result error, not an
    # unhandled KeyError that would crash the agent loop.
    result, summary = execute_tool("get_definition", ctx, {})
    assert "error" in result
    assert "failed" in summary
