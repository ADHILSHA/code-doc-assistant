"""POST /api/eval/run (SPEC.md §5, §6 Phase 4 task 2), through the real
ASGI app — mirrors test_api.py's pattern (index via HTTP, then exercise
the endpoint), all providers faked, zero network calls.
"""

from __future__ import annotations

from pathlib import Path

from app.providers.llm import FakeLLMProvider, ToolUseResponse

from .test_api import _make_client, _wait_for_job


def _scripted_llm(n: int = 30) -> FakeLLMProvider:
    return FakeLLMProvider(tool_responses=[ToolUseResponse(text=f"Answer {i}.", tool_calls=[]) for i in range(n)])


def _index_mini_repo(mini_repo_path: Path, tmp_path: Path, *, eval_report_dir: Path):
    client, settings = _make_client(tmp_path, llm_provider=_scripted_llm())
    settings.eval_report_dir = eval_report_dir
    resp = client.post("/api/repos", json={"source": str(mini_repo_path)})
    body = resp.json()
    job = _wait_for_job(client, body["job_id"])
    assert job["state"] == "succeeded", job
    return client, body["repo_id"]


def test_eval_run_endpoint_returns_full_report(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path, eval_report_dir=tmp_path / "eval_report")

    resp = client.post("/api/eval/run", json={"repo_id": repo_id, "suite": "mini_repo"})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["repo"] == "mini_repo"
    assert report["repo_id"] == repo_id
    assert report["n_questions"] == 25
    assert "overall" in report
    assert "by_route" in report


def test_eval_run_endpoint_unknown_repo_404(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/eval/run", json={"repo_id": "does-not-exist", "suite": "mini_repo"})
    assert resp.status_code == 404


def test_eval_run_endpoint_unknown_suite_400(mini_repo_path: Path, tmp_path: Path):
    client, repo_id = _index_mini_repo(mini_repo_path, tmp_path, eval_report_dir=tmp_path / "eval_report")
    resp = client.post("/api/eval/run", json={"repo_id": repo_id, "suite": "does-not-exist"})
    assert resp.status_code == 400


def test_eval_run_endpoint_repo_not_ready_409(tmp_path: Path):
    client, _settings = _make_client(tmp_path)
    resp = client.post("/api/repos", json={"source": str(tmp_path / "does-not-exist")})
    repo_id = resp.json()["repo_id"]
    resp = client.post("/api/eval/run", json={"repo_id": repo_id, "suite": "mini_repo"})
    assert resp.status_code == 409
