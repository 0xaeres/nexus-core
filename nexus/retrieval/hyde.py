"""Hypothetical Document Embeddings (HyDE).

For complex queries, ask a light LLM to write a brief hypothetical passage that
would answer the query, then embed that passage instead of (or alongside) the raw
query. This shifts the query vector closer to the kind of language used in the
indexed corpus.
"""

from __future__ import annotations

import httpx

_PROMPT_CODE = (
    "You are helping a code search engine. The user is looking for code that "
    "addresses their question. Write a SHORT (3-6 lines) hypothetical code "
    "snippet — function or method, no commentary — that would answer it. "
    "If you do not know the exact API, invent a plausible one in the relevant "
    "language. Output the snippet only, no markdown fences."
)

_PROMPT_TEXT = (
    "You are helping a documentation search engine. Write a SHORT (2-4 sentences) "
    "hypothetical passage that would answer the user's question if it existed in "
    "the corpus. Be specific and concrete. Output the passage only, no commentary."
)


class HydeClient:
    """Ollama client for hypothetical document generation."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        model: str = "qwen2.5:3b",
        timeout_s: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(self, query: str, *, mode: str = "auto") -> str | None:
        """Return a hypothetical passage, or None if the LLM is unreachable."""
        prompt = _PROMPT_CODE if mode == "code" else _PROMPT_TEXT
        body = f"{prompt}\n\nQUESTION:\n{query}\n\nANSWER:"
        try:
            resp = await self._client.post(
                "/api/generate",
                json={
                    "model": self.model,
                    "prompt": body,
                    "stream": False,
                    "options": {"temperature": 0.3, "num_predict": 200},
                },
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        text = resp.json().get("response", "").strip()
        return text or None
