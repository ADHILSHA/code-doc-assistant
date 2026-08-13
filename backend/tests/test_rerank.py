from __future__ import annotations

from app.models import RetrievedChunk
from app.providers.llm import FakeLLMProvider
from app.retrieval.rerank import merge_adjacent, rerank


def _chunk(chunk_id: int, path: str = "a.py", start: int = 1, end: int = 5, content: str = "x") -> RetrievedChunk:
    return RetrievedChunk(chunk_id=chunk_id, file_path=path, start_line=start, end_line=end, content=content)


def test_rerank_passes_through_when_already_at_or_under_keep_n():
    candidates = [_chunk(i) for i in range(1, 6)]
    result = rerank(FakeLLMProvider(), "q", candidates, keep_n=10)
    assert result == candidates


def test_rerank_reorders_and_truncates_per_llm_ranking():
    candidates = [_chunk(i) for i in range(1, 21)]
    fake = FakeLLMProvider(responses=["3, 1, 5"])
    result = rerank(fake, "q", candidates, keep_n=3)
    assert [c.chunk_id for c in result] == [3, 1, 5]


def test_rerank_ignores_out_of_range_and_duplicate_numbers():
    candidates = [_chunk(i) for i in range(1, 21)]
    fake = FakeLLMProvider(responses=["2, 2, 99, 4"])
    result = rerank(fake, "q", candidates, keep_n=5)
    assert [c.chunk_id for c in result] == [2, 4]


def test_rerank_fails_open_to_original_order_on_unparseable_response():
    candidates = [_chunk(i) for i in range(1, 21)]
    fake = FakeLLMProvider(responses=["I cannot rank these."])
    result = rerank(fake, "q", candidates, keep_n=3)
    assert [c.chunk_id for c in result] == [1, 2, 3]


def test_merge_adjacent_combines_contiguous_same_file_chunks():
    a = _chunk(1, "x.py", 1, 10, "AAA")
    b = _chunk(2, "x.py", 11, 20, "BBB")
    merged = merge_adjacent([a, b])
    assert len(merged) == 1
    assert merged[0].start_line == 1
    assert merged[0].end_line == 20
    assert merged[0].content == "AAA\nBBB"


def test_merge_adjacent_keeps_distant_same_file_chunks_separate():
    a = _chunk(1, "x.py", 1, 10, "AAA")
    b = _chunk(2, "x.py", 100, 110, "BBB")
    merged = merge_adjacent([a, b])
    assert len(merged) == 2


def test_merge_adjacent_keeps_different_files_separate():
    a = _chunk(1, "x.py", 1, 10, "AAA")
    b = _chunk(2, "y.py", 1, 10, "BBB")
    merged = merge_adjacent([a, b])
    assert len(merged) == 2
    assert {m.file_path for m in merged} == {"x.py", "y.py"}


def test_merge_adjacent_preserves_symbol_name_from_either_side():
    a = _chunk(1, "x.py", 1, 10, "AAA")
    b = _chunk(2, "x.py", 11, 20, "BBB")
    b.symbol_name = "named_thing"
    merged = merge_adjacent([a, b])
    assert merged[0].symbol_name == "named_thing"


def test_merge_adjacent_empty_list():
    assert merge_adjacent([]) == []
