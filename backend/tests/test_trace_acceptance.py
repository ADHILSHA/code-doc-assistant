"""SPEC.md §6 Phase 3 acceptance criterion: "A cross-file trace question
produces an answer citing >=3 files in correct call order."

mini_repo's user-creation flow is a genuine four-file call chain
(tests/test_service.py -> src/users/service.py -> src/auth/auth.py ->
src/auth/crypto.py — see src/auth/crypto.py's own docstring), not one
fabricated only for this test. Since FakeLLMProvider can't demonstrate real
model *reasoning* (SPEC.md §7.2 forbids network calls in tests), this
verifies the two things actually within this codebase's control:
1. the retrieval pipeline (hybrid search -> graph expansion) genuinely
   surfaces all of the chain's files as context before the agent even
   starts reasoning — not scripted, a real multi-hop retrieval result;
2. once a final answer cites those files, the pipeline preserves citation
   order and validates every citation correctly end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

from app import jobs
from app.db import get_registry_connection, get_repo_connection
from app.generation.answer import generate_answer_events
from app.generation.citations import RepoContext
from app.providers.embeddings import FakeEmbeddingProvider
from app.providers.llm import FakeLLMProvider, ToolUseResponse

from .conftest import MINI_REPO, make_settings


def _index_mini_repo(tmp_path: Path):
    settings = make_settings(tmp_path, allow_local_repos=True)
    repo_id, job_id = jobs.create_repo_and_job(str(MINI_REPO), settings)
    jobs.run_index_job(job_id, repo_id, str(MINI_REPO), settings)
    conn = get_repo_connection(repo_id, FakeEmbeddingProvider.dim, settings)
    registry_conn = get_registry_connection(settings)
    local_path = registry_conn.execute(
        "SELECT local_path FROM repos WHERE id = ?", (repo_id,)
    ).fetchone()["local_path"]
    return conn, settings, Path(local_path)


def _by_event(events: list[dict], name: str) -> list[dict]:
    return [json.loads(e["data"]) for e in events if e["event"] == name]


def test_retrieval_surfaces_the_full_multihop_chain_before_the_agent_runs(tmp_path: Path):
    """Real retrieval, not scripted: hybrid search seeded on the
    create_user/hash_password flow, expanded across the call graph, must
    surface all of service.py / auth.py / crypto.py as context — proving
    the "never starts blind" seed genuinely spans the chain."""
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)

    fake_llm = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text=(
                    "`create_user` [src/users/service.py:28-32] calls `hash_password` "
                    "[src/auth/auth.py:18-22], which derives the key via `derive_key` "
                    "[src/auth/crypto.py:16-19]."
                ),
                tool_calls=[],
            )
        ]
    )
    events = list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), fake_llm, repo_context, repo_root,
            question="Trace what happens when create_user hashes a password, from the service call "
            "down to the crypto primitive",
        )
    )

    sources = _by_event(events, "sources")[0]["chunks"]
    source_paths = {c["path"] for c in sources}
    assert {"src/users/service.py", "src/auth/auth.py", "src/auth/crypto.py"} <= source_paths, (
        f"expected the multi-hop chain's files in the retrieved context, got {source_paths}"
    )


def test_final_answer_citing_three_files_preserves_correct_call_order(tmp_path: Path):
    conn, settings, repo_root = _index_mini_repo(tmp_path)
    repo_context = RepoContext(source_type="local", owner_repo=None, commit_sha=None)

    fake_llm = FakeLLMProvider(
        tool_responses=[
            ToolUseResponse(
                text=(
                    "`create_user` [src/users/service.py:28-32] calls `hash_password` "
                    "[src/auth/auth.py:18-22], which calls `derive_key` [src/auth/crypto.py:16-19] "
                    "to actually derive the PBKDF2 key."
                ),
                tool_calls=[],
            )
        ]
    )
    events = list(
        generate_answer_events(
            conn, settings, FakeEmbeddingProvider(), fake_llm, repo_context, repo_root,
            question="Trace the call chain from create_user to the crypto primitive",
        )
    )

    done = _by_event(events, "done")[0]
    assert done["route"] == "explain"

    citations = _by_event(events, "citations")[0]["citations"]
    assert len(citations) >= 3
    paths_in_order = [c["path"] for c in citations]
    assert paths_in_order == [
        "src/users/service.py",
        "src/auth/auth.py",
        "src/auth/crypto.py",
    ], f"citation order didn't match the real call order: {paths_in_order}"

    # Every citation resolved (none stripped as invalid) — real file/line
    # bounds, not just present in the answer text.
    answer_text = "".join(json.loads(e["data"])["text"] for e in events if e["event"] == "token")
    assert answer_text.count("src/") >= 3
