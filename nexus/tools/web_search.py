"""Web search adapter for the Curator agent.

Two providers wired:
- `tavily`     - https://tavily.com (set TAVILY_API_KEY); fast, AI-tuned results.
- `none`       - no-op (returns []); offline / dev default.

Easy to extend with DuckDuckGo / SerpAPI / Brave; each provider just needs to
return `list[WebResult]`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    snippet: str


class WebSearchClient:
    def __init__(self, *, provider: str = "auto", timeout_s: float = 15.0):
        if provider == "auto":
            provider = "tavily" if os.environ.get("TAVILY_API_KEY") else "none"
        self.provider = provider
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, max_results: int = 5) -> list[WebResult]:
        if self.provider == "tavily":
            return await self._tavily(query, max_results)
        return []

    async def _tavily(self, query: str, max_results: int) -> list[WebResult]:
        key = os.environ.get("TAVILY_API_KEY")
        if not key:
            return []
        try:
            r = await self._client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "advanced",
                    "include_answer": False,
                },
            )
        except httpx.HTTPError as e:
            log.warning("tavily failed: %s", e)
            return []
        if r.status_code != 200:
            log.warning("tavily status %d: %s", r.status_code, r.text[:120])
            return []
        items = (r.json() or {}).get("results", []) or []
        out: list[WebResult] = []
        for it in items[:max_results]:
            out.append(
                WebResult(
                    title=str(it.get("title", "")),
                    url=str(it.get("url", "")),
                    snippet=str(it.get("content", ""))[:600],
                )
            )
        return out
