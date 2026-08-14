"""Golden-set loader (SPEC.md §6 Phase 4 task 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.eval.golden import GoldenSetError, golden_set_path, load_golden_set

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
MINI_REPO_GOLDEN = _PROJECT_ROOT / "eval" / "golden" / "mini_repo.yaml"


def test_mini_repo_golden_set_loads_and_has_25_questions_5_per_route():
    questions = load_golden_set(MINI_REPO_GOLDEN)
    assert len(questions) == 25

    from collections import Counter

    counts = Counter(q.route for q in questions)
    assert counts == {
        "locate": 5, "dependencies": 5, "endpoints": 5, "explain": 5, "overview": 5,
    }


def test_mini_repo_golden_set_ids_are_unique():
    questions = load_golden_set(MINI_REPO_GOLDEN)
    ids = [q.id for q in questions]
    assert len(ids) == len(set(ids))


def test_golden_set_path_helper():
    assert golden_set_path(_PROJECT_ROOT / "eval" / "golden", "mini_repo") == MINI_REPO_GOLDEN


def test_load_golden_set_missing_file_raises(tmp_path: Path):
    with pytest.raises(GoldenSetError, match="not found"):
        load_golden_set(tmp_path / "does_not_exist.yaml")


def test_load_golden_set_empty_list_raises(tmp_path: Path):
    p = tmp_path / "empty.yaml"
    p.write_text("[]\n")
    with pytest.raises(GoldenSetError, match="non-empty"):
        load_golden_set(p)


def test_load_golden_set_missing_required_field_raises(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("- id: q1\n  question: 'hi'\n")  # missing route
    with pytest.raises(GoldenSetError, match="missing required field"):
        load_golden_set(p)


def test_load_golden_set_invalid_route_raises(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text("- id: q1\n  question: 'hi'\n  route: not_a_real_route\n")
    with pytest.raises(GoldenSetError, match="invalid route"):
        load_golden_set(p)


def test_load_golden_set_duplicate_id_raises(tmp_path: Path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "- id: dup\n  question: 'a'\n  route: locate\n"
        "- id: dup\n  question: 'b'\n  route: overview\n"
    )
    with pytest.raises(GoldenSetError, match="duplicate"):
        load_golden_set(p)


def test_load_golden_set_defaults_for_optional_fields(tmp_path: Path):
    p = tmp_path / "minimal.yaml"
    p.write_text("- id: q1\n  question: 'What does this do?'\n  route: overview\n")
    (question,) = load_golden_set(p)
    assert question.expected_files == []
    assert question.expected_substrings == []
    assert question.notes is None
