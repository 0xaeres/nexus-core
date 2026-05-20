"""Reranker client — Jina Reranker v3 served by llama-server.

llama-server `--reranking` exposes:
  POST /reranking  { "query": "...", "documents": ["...", ...] }
returning an `{"results": [{"index": i, "relevance_score": s}, ...]}` shape
similar to Cohere's rerank API.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class RerankResult:
    index: int  # index into the original `documents` list
    score: float


class RerankerError(RuntimeError):
    pass


class RerankerClient:
    def __init__(self, base_url: str = "http://localhost:8081", *, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def rerank(
        self, query: str, documents: list[str], *, top_k: int | None = None
    ) -> list[RerankResult]:
        """Score every document; return them ordered by score (desc)."""
        if not documents:
            return []
        body: dict[str, object] = {"query": query, "documents": documents}
        if top_k is not None:
            body["top_n"] = top_k
        try:
            resp = await self._client.post("/reranking", json=body)
        except httpx.HTTPError as e:
            raise RerankerError(f"reranker request failed: {e}") from e
        if resp.status_code != 200:
            raise RerankerError(
                f"reranker returned {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()
        results = payload.get("results", payload.get("data", []))
        out = [
            RerankResult(
                index=r.get("index", i),
                score=float(r.get("relevance_score", r.get("score", 0.0))),
            )
            for i, r in enumerate(results)
        ]
        out.sort(key=lambda r: r.score, reverse=True)
        if top_k is not None:
            out = out[:top_k]
        return out

    async def health(self) -> bool:
        try:
            r = await self._client.get("/health")
            return r.status_code == 200
        except httpx.HTTPError:
            return False
