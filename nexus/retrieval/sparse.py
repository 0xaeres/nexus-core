"""BM25 sparse encoder — wraps fastembed's Qdrant/bm25 tokenizer.

Produces TF-weighted sparse vectors; Qdrant applies IDF server-side via the
`Modifier.IDF` setting on the collection's sparse vector params.

Module-level singletons because the underlying fastembed model loads a vocab
file on first construction.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache

from fastembed import SparseTextEmbedding


@dataclass(frozen=True)
class SparseVector:
    indices: list[int]
    values: list[float]


@lru_cache(maxsize=1)
def _bm25_model() -> SparseTextEmbedding:
    # First construction downloads the vocab file (~few MB).
    return SparseTextEmbedding("Qdrant/bm25")


def encode_passages(texts: Iterable[str]) -> list[SparseVector]:
    """Encode documents for indexing."""
    model = _bm25_model()
    out: list[SparseVector] = []
    for emb in model.passage_embed(list(texts)):
        out.append(SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist()))
    return out


def encode_query(text: str) -> SparseVector:
    """Encode a single query (uses a query-side tokenisation pass)."""
    model = _bm25_model()
    emb = next(iter(model.query_embed([text])))
    return SparseVector(indices=emb.indices.tolist(), values=emb.values.tolist())


# ---- async wrappers (fastembed is sync; run in a thread to avoid blocking the loop)


async def aencode_passages(texts: Iterable[str]) -> list[SparseVector]:
    return await asyncio.to_thread(encode_passages, list(texts))


async def aencode_query(text: str) -> SparseVector:
    return await asyncio.to_thread(encode_query, text)
