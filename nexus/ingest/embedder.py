"""Embedder client — Jina v4 served by llama-server (Apple Silicon, Metal).

Jina v4 separates **task** (retrieval / classification / clustering / …) and
**modality** (text / code). We use two modes, mapped to Qdrant named vectors:

| modality | task           | Qdrant vector |
|----------|----------------|---------------|
| code     | retrieval      | dense_code    |
| text     | retrieval      | dense_text    |

llama-server with `--embedding` does not natively switch LoRA adapters per
request, so we apply Jina's documented instruction prefix to the input string.
This is consistent with how transformers-jina v4 internally formats inputs.
"""

from __future__ import annotations

import asyncio
from typing import Literal

import httpx

from nexus.ingest.models import Chunk, EmbeddedChunk

VectorName = Literal["dense_code", "dense_text"]
Modality = Literal["passage", "query"]

# Jina v4 instruction prefixes (per the model card). Keep in sync with the served GGUF.
_PREFIXES: dict[tuple[VectorName, Modality], str] = {
    ("dense_code", "passage"): "Represent the code for retrieval: ",
    ("dense_code", "query"): "Represent the question for retrieving relevant code: ",
    ("dense_text", "passage"): "Represent the document for retrieval: ",
    ("dense_text", "query"): "Represent the question for retrieving relevant documents: ",
}


class EmbedderError(RuntimeError):
    pass


class EmbedderClient:
    """Thin async client. Construct once, reuse across the ingestion pipeline."""

    def __init__(self, base_url: str, *, timeout_s: float = 30.0, batch_size: int = 32):
        self.base_url = base_url.rstrip("/")
        self.batch_size = batch_size
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def embed(
        self, texts: list[str], *, vector: VectorName, modality: Modality = "passage"
    ) -> list[list[float]]:
        """Return one vector per input string. Batched internally."""
        if not texts:
            return []
        prefix = _PREFIXES[(vector, modality)]
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = [prefix + t for t in texts[i : i + self.batch_size]]
            out.extend(await self._call(batch))
        return out

    async def _call(self, inputs: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.post(
                "/v1/embeddings",
                json={"input": inputs, "model": "jina-embeddings-v4"},
            )
        except httpx.HTTPError as e:
            raise EmbedderError(f"embedder request failed: {e}") from e
        if resp.status_code != 200:
            raise EmbedderError(
                f"embedder returned {resp.status_code}: {resp.text[:200]}"
            )
        data = resp.json().get("data", [])
        # OpenAI-compat response keeps input order via the `index` field
        ordered = sorted(data, key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in ordered]

    # ------------------------------------------------------------------ helpers

    async def embed_chunks(self, chunks: list[Chunk]) -> list[EmbeddedChunk]:
        """Compute the right named-vector for each chunk based on its kind."""
        code = [c for c in chunks if c.kind.value == "code"]
        docs = [c for c in chunks if c.kind.value == "doc"]

        code_vecs, doc_vecs = await asyncio.gather(
            self.embed([c.text_for_embedding() for c in code], vector="dense_code"),
            self.embed([c.text_for_embedding() for c in docs], vector="dense_text"),
        )
        result: list[EmbeddedChunk] = []
        for c, v in zip(code, code_vecs, strict=True):
            result.append(EmbeddedChunk(chunk=c, vector=v, vector_name="dense_code"))
        for c, v in zip(docs, doc_vecs, strict=True):
            result.append(EmbeddedChunk(chunk=c, vector=v, vector_name="dense_text"))
        return result

    async def embed_query(self, text: str, *, vector: VectorName) -> list[float]:
        vecs = await self.embed([text], vector=vector, modality="query")
        return vecs[0]

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False
