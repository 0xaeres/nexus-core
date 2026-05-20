"""Contextual chunk enrichment - ADR-010.

For each chunk, ask a light LLM to write 1-2 sentences describing where the
chunk sits in the surrounding resource (file path, class/function, doc section).
Prepended to the chunk's text only when sent to the embedder; chunk.content
itself is never mutated. Per-source-type toggle: docs default on, code default off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import httpx

from nexus.ingest.models import Chunk, ChunkKind

_DEFAULT_PROMPT = (
    "You annotate retrieval corpora. In ONE short sentence (max 30 words), describe "
    "where this excerpt lives so that future retrieval queries can find it. Mention "
    "the file path and the enclosing structure (class/function/heading) when known. "
    "Do not summarise the body; only describe the location."
)


class EnricherError(RuntimeError):
    pass


class ContextualEnricher:
    """Calls Ollama's /api/generate with a small instruction-tuned model."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        model: str = "qwen2.5:3b",
        enrich_code: bool = False,
        enrich_docs: bool = True,
        concurrency: int = 4,
        timeout_s: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.enrich_code = enrich_code
        self.enrich_docs = enrich_docs
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout_s)
        self._sem = asyncio.Semaphore(concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _should_enrich(self, chunk: Chunk) -> bool:
        if chunk.kind is ChunkKind.CODE:
            return self.enrich_code
        return self.enrich_docs

    # ------------------------------------------------------------------ batch

    async def enrich(self, chunks: Iterable[Chunk]) -> list[Chunk]:
        """Return chunks (possibly with `context_summary` populated)."""
        chunk_list = list(chunks)
        targets = [c for c in chunk_list if self._should_enrich(c)]
        if not targets:
            return chunk_list
        summaries = await asyncio.gather(*[self._summary_for(c) for c in targets])
        for c, summary in zip(targets, summaries, strict=True):
            if summary:
                c.context_summary = summary
        return chunk_list

    async def _summary_for(self, chunk: Chunk) -> str | None:
        async with self._sem:
            prompt = self._render_prompt(chunk)
            try:
                resp = await self._client.post(
                    "/api/generate",
                    json={
                        "model": self.model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2, "num_predict": 80},
                    },
                )
            except httpx.HTTPError:
                return None
            if resp.status_code != 200:
                return None
            text = resp.json().get("response", "").strip()
            return text or None

    @staticmethod
    def _render_prompt(chunk: Chunk) -> str:
        head = _DEFAULT_PROMPT
        meta_lines = [
            f"FILE: {chunk.resource.uri}",
            f"KIND: {chunk.kind.value}",
            f"LINES: {chunk.start_line}-{chunk.end_line}",
        ]
        if chunk.context_path:
            meta_lines.append(f"STRUCT: {chunk.context_path}")
        meta = "\n".join(meta_lines)
        snippet = chunk.content
        if len(snippet) > 1200:
            snippet = snippet[:1200] + "\n…"
        return f"{head}\n\n{meta}\n\nEXCERPT:\n```\n{snippet}\n```\n\nLOCATION:"

    async def health(self) -> bool:
        try:
            r = await self._client.get("/api/tags")
            return r.status_code == 200
        except httpx.HTTPError:
            return False
