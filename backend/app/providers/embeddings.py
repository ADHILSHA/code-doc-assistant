"""EmbeddingProvider protocol + adapters (SPEC.md §7.2).

Business logic imports `EmbeddingProvider` / `get_embedding_provider`, never a
vendor SDK directly. Real adapters lazy-import their SDK so the app can start
(and `FakeEmbeddingProvider` can run in tests) without those optional
dependencies installed.
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Protocol

from app.config import Settings


class EmbeddingProvider(Protocol):
    dim: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FakeEmbeddingProvider:
    """Deterministic hash-based vectors — zero deps, zero network.

    SPEC.md §7.2: "no test may make a network call" — this is the only
    embedding provider tests are allowed to use.
    """

    dim = 32

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in text.lower().split():
            digest = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
            idx = digest % self.dim
            sign = 1.0 if (digest // self.dim) % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _retry(fn, *, attempts: int = 3, base_delay: float = 1.0):
    """Small exponential-backoff retry for real (network) embedding calls,
    per SPEC.md §7.4."""
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - provider SDKs raise varied types
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


_VOYAGE_DIMS = {"voyage-code-3": 1024}


class VoyageEmbeddingProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import voyageai  # lazy import: optional dependency

        self._client = voyageai.Client(api_key=api_key)
        self._model = model
        self.dim = _VOYAGE_DIMS.get(model, 1024)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        result = _retry(
            lambda: self._client.embed(texts, model=self._model, input_type="document")
        )
        return result.embeddings

    def embed_query(self, text: str) -> list[float]:
        result = _retry(lambda: self._client.embed([text], model=self._model, input_type="query"))
        return result.embeddings[0]


_OPENAI_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}


class OpenAIEmbeddingProvider:
    def __init__(self, api_key: str, model: str) -> None:
        import openai  # lazy import: optional dependency

        self._client = openai.OpenAI(api_key=api_key)
        self._model = model
        self.dim = _OPENAI_DIMS.get(model, 1536)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        resp = _retry(lambda: self._client.embeddings.create(input=texts, model=self._model))
        return [d.embedding for d in resp.data]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def get_embedding_provider(settings: Settings) -> EmbeddingProvider:
    kind = settings.embedding_provider
    if kind == "fake":
        return FakeEmbeddingProvider()
    if kind == "voyage":
        if not settings.voyage_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=voyage requires VOYAGE_API_KEY")
        return VoyageEmbeddingProvider(settings.voyage_api_key, settings.voyage_embedding_model)
    if kind == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        return OpenAIEmbeddingProvider(settings.openai_api_key, settings.openai_embedding_model)
    raise ValueError(f"Unknown embedding provider: {kind}")
